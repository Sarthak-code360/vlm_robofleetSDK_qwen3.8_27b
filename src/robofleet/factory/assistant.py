"""The factory's AI worker: a chat that turns plain words into factory actions.

    "print a rocket fin"                 -> starts a free machine that has that file
    "print the cap on PRINTER-2, RBT-003 delivers" -> that machine, that robot
    "make 2 rockets"                       -> a production run of 10 units
    "how many robots are online?"             -> an answer from the live factory state

The model (shown to users under the assistant's name, default "StarForge AI") chooses the action and writes the
reply. Everything it chooses is checked in code before anything moves. Progress of the tasks it started, and alerts
(no robot available, no machine connected), are posted back into the chat.
"""

from __future__ import annotations

import json
import re
import time

from ..model import ModelError
from .catalog import machine_label
from .machines import is_live

DEFAULT_ASSISTANT = "StarForge AI"
NONE = "none"
CHAT_LIMIT = 300
STATUS_WORDS = {"status", "how", "what", "which", "many", "online", "available", "doing", "progress", "report", "who"}


_CLOSER = re.compile(
    r"\s*(?:is there anything else|anything else (?:i can|you(?:'d| would) like)|let me know if|feel free to|"
    r"do you (?:need|want) (?:anything|any)|would you like (?:me to|to know|anything)|how (?:else )?can i (?:help|assist)|"
    r"if you (?:need|have) (?:anything|any (?:other|more|further)))[^.!?]*[.!?]*\s*$", re.IGNORECASE)


def clean_reply(text: str) -> str:
    """Drops the chat-model habit of closing every answer with 'Is there anything else...?'."""
    text = str(text).strip()
    for _ in range(3):
        cleaned = _CLOSER.sub("", text).strip()
        if cleaned == text or not cleaned:
            break
        text = cleaned
    return text


class AssistantMixin:
    """Chat for :class:`~robofleet.factory.runtime.Factory` (uses its lock, state and job/task helpers)."""

    # ------------------------------------------------------------------ messages

    def _chat_post(self, role: str, text: str, kind: str = "reply", **refs) -> dict:
        self._chat_seq += 1
        message = {"id": self._chat_seq, "role": role, "kind": kind, "text": text, "ts": round(time.time(), 3), **refs}
        self.chat.append(message)
        del self.chat[:-CHAT_LIMIT]
        return message

    def _chat_follow(self, kind: str, text: str, data: dict) -> None:
        """Called for every event: posts alerts, and progress of work that was started from the chat."""
        if kind == "alert":
            self._chat_post("assistant", text, "alert")
            return
        job = self.jobs.get(data.get("job_id") or "")
        task = self.tasks.get(data.get("task_id") or "")
        if task and not job:
            job = self.jobs.get(task.get("job_id") or "")
        run = self.runs.get(data.get("run_id") or "")
        if any((x or {}).get("origin") == "chat" for x in (job, task, run)):
            friendly = self._friendly(kind, text, job, task)
            if friendly:
                self._chat_post("assistant", friendly, "update")

    def _friendly(self, kind: str, text: str, job: dict | None, task: dict | None) -> str | None:
        """Plain-language progress for people who are not engineers. None = not worth a message."""
        def part(file):
            return re.sub(r"[_-]+", " ", re.sub(r"\.gcode\.3mf$", "", file or "part", flags=re.I))

        def machine_name(mid):
            m = self.machines.get(mid)
            return (m.spec.get("name") if m else None) or mid

        def robot_name(rid):
            r = self.robots.get(rid or "")
            return r["name"] if r else (rid or "A robot")

        thing = part(job["file"]) if job else "the item"
        if kind == "job" and job:
            if "started" in text:
                return f"{machine_name(job['machine_id'])} started making the {thing}."
            if "finished" in text:
                return f"The {thing} is ready. Sending a robot to collect it."
            if "FAILED" in text or "could not start" in text:
                return f"{machine_name(job['machine_id'])} could not make the {thing}." + (
                    f" ({job['error']})" if job.get("error") else "")
            return None
        if kind == "dispatch" and task:
            return f"{robot_name(task['robot_id'])} is on the way to collect the {thing}."
        if kind == "task" and task:
            if "finished" in text:
                return f"Done ✓ The {thing} was delivered to {task['to_zone']}."
            if "FAILED" in text:
                return f"{robot_name(task['robot_id'])} could not deliver the {thing}." + (
                    f" ({task['note']})" if task.get("note") else "")
            if "back in the queue" in text:
                return f"The robot went offline. Finding another robot for the {thing}."
            return None  # "picked up": the dispatch message already said it
        if kind == "run":
            return text
        return text

    # ------------------------------------------------------------------ entry point

    def chat_message(self, text: str, machine_id: str | None = None, robot_id: str | None = None,
                     document_id: str | None = None, print_file: str | None = None) -> dict:
        """Handles one chat message. Returns the new messages (the user's and the assistant's).

        ``print_file``: a model file the user attached (already uploaded to ``machine_id``): print it now if the
        machine is connected and free, otherwise report its state.
        """
        text = str(text or "").strip()[:2000] or ("Print this" if print_file else "")
        if not text:
            raise ValueError("message is empty")
        if machine_id and machine_id not in self.machines:
            raise ValueError(f"unknown machine {machine_id}")
        if robot_id and robot_id not in self.robots:
            raise ValueError(f"unknown robot {robot_id}")
        document = None
        if document_id:
            if document_id not in self.documents:
                raise KeyError(document_id)
            document = {k: self.documents[document_id][k] for k in ("id", "name")}
        if print_file:
            with self.lock:
                first = self._chat_post("user", text, machine_id=machine_id, robot_id=robot_id, print_file=print_file)["id"]
            reply = self._chat_print_attached(print_file, machine_id, robot_id)
            with self.lock:
                self._chat_post("assistant", reply, "reply")
                return {"messages": [m for m in self.chat if m["id"] >= first], "choice": {"action": "print_attached"}}
        with self.lock:
            first = self._chat_post("user", text, machine_id=machine_id, robot_id=robot_id,
                                    document=document)["id"]
            snapshot = self._chat_briefing()
        choice, stats = self._chat_interpret(text, snapshot, machine_id, robot_id, document)
        if choice.get("action") == "start_production":
            reply = self._chat_start_run(text, choice, document, clean_reply(choice.get("reply") or ""))
        else:
            with self.lock:
                reply = self._chat_act(text, choice, machine_id, robot_id, document)
        with self.lock:
            self._chat_post("assistant", reply, "reply", latency_s=stats.get("latency_s"))
            return {"messages": [m for m in self.chat if m["id"] >= first], "choice": choice}

    # ------------------------------------------------------------------ understanding

    def _chat_briefing(self) -> dict:
        machines = []
        for m in self.machines.values():
            info = m.describe()
            holding = any(j["machine_id"] == m.id and j["status"] in {"awaiting_confirmation", "starting", "running", "done"}
                          for j in self.jobs.values())
            state = "holding part" if holding and info["state"] == "idle" else info["state"]
            if info.get("progress_pct") is not None and info["state"] == "busy":
                state += f" {info['progress_pct']}%"
            machines.append({"id": m.id, "name": m.spec.get("name") or m.id, "type": machine_label(m.type),
                             "live": is_live(m), "zone": m.zone, "state": state, "files": m.list_files()})
        # compact on purpose: every token here is read by the model on every message
        robots = [f"{r['id']} {r['status']} {r['zone'] or '?'} {r['battery']}%" for r in self.robots.values()]
        active = [self.run_view(r) for r in self.runs.values() if r["status"] in {"running", "paused"}]
        return {
            "factory": self.config.get("name"), "type": self.site["type"], "zones": list(self.site["zones"]),
            "default_output_zone": self.default_output_zone, "machines": machines, "robots": robots,
            "products": list(self.products),
            "production_runs": [{k: r[k] for k in ("id", "name", "status", "units", "target", "progress_pct")} for r in active],
            "jobs": {s: sum(j["status"] == s for j in self.jobs.values())
                     for s in ("awaiting_confirmation", "running", "done", "delivered", "failed")},
            "tasks_waiting_for_robot": sum(t["status"] == "pending" for t in self.tasks.values()),
        }

    def _chat_interpret(self, text: str, briefing: dict, machine_id, robot_id, document) -> tuple[dict, dict]:
        files = sorted({f for m in briefing["machines"] for f in m["files"]})
        zones = briefing["zones"]
        schema = {
            "type": "object", "additionalProperties": False,
            "required": ["action", "reply"],  # the rest only when needed: fewer tokens to generate = faster replies
            "properties": {
                "action": {"type": "string", "enum": ["run_machine", "move_item", "start_production", "answer"]},
                "machine_id": {"type": "string", "enum": [m["id"] for m in briefing["machines"]] + [NONE]},
                "file": {"type": "string", "enum": files + [NONE]},
                "robot_id": {"type": "string", "enum": list(self.robots) + [NONE]},
                "from_zone": {"type": "string", "enum": zones + [NONE]},
                "to_zone": {"type": "string", "enum": zones + [NONE]},
                "product": {"type": "string"},
                "quantity": {"type": "integer", "minimum": 0, "maximum": 100000},
                "reply": {"type": "string"},
            },
        }
        hints = []
        if machine_id:
            hints.append(f"The user selected machine {machine_id}.")
        if robot_id:
            hints.append(f"The user selected robot {robot_id}.")
        if document:
            hints.append(f"Attached process document: {document['name']}.")
        system = (
            f"You are {self.assistant_name}, the AI worker that runs this autonomous factory. You start machines, "
            "assign robots and answer questions about the factory. Choose ONE action:\n"
            "- run_machine: make ONE part now. Pick an idle machine (not 'busy' or 'holding part') and one of ITS files. "
            "to_zone = where the part goes (default_output_zone if not said). robot_id only if the user named a robot.\n"
            "- move_item: a robot carries something from from_zone to to_zone.\n"
            "- start_production: make one or more complete products (product = a name from 'products' when it fits, "
            "quantity = how many). A complete product goes through every station in order.\n"
            "- answer: a question, greeting or anything else. Factory questions: answer from the factory data. "
            "Other questions (e.g. maths): just answer.\n"
            "If the user asks to print or make something and no listed file matches it, use run_machine with file "
            "\"none\" (the system then reports the printers and asks for the model file). Machines with live=true are "
            "real hardware; the others are simulated.\n"
            "Only include the fields an action needs. 'reply' is your message to the user: short and direct, 1-2 "
            "sentences. Never end with a follow-up question or an offer of more help (no 'Is there anything else...', "
            "'Let me know if...'). If nothing can do a task, say what is missing (e.g. add a machine that has the file, "
            "or add a robot). Do not invent IDs. JSON only."
            + ("\nFactory owner's instructions:\n- " + "\n- ".join(self.assistant_instructions)
               if self.assistant_instructions else ""))
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": "Factory now:\n" + json.dumps(briefing, separators=(",", ":"))
                     + "\n\nRecent conversation:\n" + self._chat_history_text()
                     + "\n\nUser: " + text + ("\n" + " ".join(hints) if hints else "")}]
        if self.model is not None:
            try:
                return self.model.chat_json(messages, schema, "factory_chat", timeout=300, max_tokens=400)
            except ModelError as e:
                self._event("model", f"{self.assistant_name} model unavailable, using keyword matching: {e}")
        return self._chat_keywords(text, briefing), {"model": "keywords", "latency_s": 0}

    def _chat_history_text(self) -> str:
        lines = [f"{m['role']}: {m['text'][:150]}" for m in self.chat[-7:-1] if m["kind"] == "reply" or m["role"] == "user"]
        return "\n".join(lines) or "(none)"

    def _chat_keywords(self, text: str, briefing: dict) -> dict:
        """No model: recognise print requests, "make N ...", and status questions."""
        from .runtime import _keyword_choice

        lower = text.lower()
        words = set(re.findall(r"[a-z0-9]+", lower))
        base = {"action": "answer", "machine_id": NONE, "file": NONE, "robot_id": NONE, "from_zone": NONE,
                "to_zone": NONE, "product": "", "quantity": 0, "reply": ""}
        for m in briefing["machines"]:
            if m["id"].lower() in lower:
                base["machine_id"] = m["id"]
        for rid in self.robots:
            if re.search(rf"\b{re.escape(rid.lower())}\b", lower):
                base["robot_id"] = rid
        number = re.search(r"\b(\d{1,5})\b", lower)
        if words & {"make", "produce", "build", "manufacture"} and self.match_product(text):
            qty = int(number.group(1)) if number else 1
            return dict(base, action="start_production", quantity=qty, product=self.match_product(text))
        if number and int(number.group(1)) > 1 and words & {"make", "produce", "build", "manufacture"}:
            return dict(base, action="start_production", quantity=int(number.group(1)),
                        product=re.sub(r"\b(make|produce|build|manufacture|please|units?|of)\b|\d+", " ", lower).strip().title())
        machines = [dict(m, state="idle" if m["state"] == "idle" else "busy") for m in briefing["machines"]]
        pick = _keyword_choice(text, machines, briefing["default_output_zone"])
        if pick["action"] == "run_machine":
            return dict(base, action="run_machine", machine_id=base["machine_id"] if base["machine_id"] != NONE
                        else pick["machine_id"], file=pick["file"], to_zone=pick["to_zone"])
        if words & {"print", "make", "produce"}:
            files = sorted({f for m in briefing["machines"] for f in m["files"]})
            for f in files:
                if set(re.findall(r"[a-z0-9]+", f.lower().replace(".gcode.3mf", ""))) & words - {"the", "a", "print"}:
                    return dict(base, action="run_machine", file=f, to_zone=briefing["default_output_zone"] or NONE)
            return dict(base, action="run_machine")  # a task, but no matching file: the reply says what is missing
        return base

    # ------------------------------------------------------------------ acting (checked in code)

    def _chat_act(self, text: str, choice: dict, machine_id, robot_id, document) -> str:
        action = choice.get("action")
        said = clean_reply(choice.get("reply") or "")
        robot_id = robot_id or (choice.get("robot_id") if choice.get("robot_id") in self.robots else None)
        if action == "answer":
            return said or self._status_summary()
        if action == "move_item":
            src, dst = choice.get("from_zone"), choice.get("to_zone")
            if src not in self.site["zones"] or dst not in self.site["zones"] or src == dst:
                return "I could not tell where to move it from and to. Name the two zones, e.g. 'move the box from " \
                       f"{next(iter(self.site['zones']))} to {self.default_output_zone}'."
            task = self._new_task(src, dst, f"Carry the item requested ({text}) from {src} to {dst}.",
                                  document=document)
            task.update(origin="chat", preferred_robot=robot_id)
            return self._join(said, f"Created {task['id']}: {src} → {dst}"
                              + (f", assigned to {robot_id}." if robot_id else ". I'll assign the best robot.")
                              + self._robot_warning())
        if action != "run_machine":
            return said or "I can start machines, run production and move parts. What should I do?"

        if not self.machines:
            return "There are no machines yet. Add one in **Settings → Machines**, then ask me again."
        file = choice.get("file")
        candidates = [self.machines[machine_id]] if machine_id else (
            [self.machines[choice["machine_id"]]] if choice.get("machine_id") in self.machines else [])
        candidates += [m for m in self.machines.values() if m not in candidates and not machine_id]
        with_file = [m for m in candidates if file in m.list_files()]
        wants_print = bool(re.search(r"\bprint", text, re.I))
        if not with_file and wants_print and any(m.type == "3d_printer" for m in self.machines.values()):
            return self._printer_report(ask_for_file=True, thing=self._thing(text))
        if not with_file:
            where = f"{machine_id} has" if machine_id else "no machine has"
            known = sorted({f for m in self.machines.values() for f in m.list_files()})
            nice = ", ".join(re.sub(r"[_-]+", " ", re.sub(r"\.gcode\.3mf$", "", f, flags=re.I)) for f in known)
            return f"I can't make that yet: {where} a file for it. I can make: {nice or 'nothing yet'}. " \
                   "Add the part's print file to the machine to make it possible."
        free = [m for m in with_file if m.state() == "idle" and not self._machine_holding(m.id)]
        if not free:
            return "All machines that can make that are busy right now. Ask me again in a moment, " \
                   "or say 'make 5 …' and I'll queue them up as a production run."
        machine = free[0]
        to_zone = choice.get("to_zone") if choice.get("to_zone") in self.site["zones"] else self.default_output_zone
        job = self._new_job(text, machine.id, file, to_zone, said[:200], document, "normal", self.require_confirmation)
        job.update(origin="chat", preferred_robot=robot_id)
        self._event("request", f"{self.assistant_name}: {job['id']} on {machine.id} → {file}, deliver to {to_zone}",
                    job_id=job["id"])
        thing = re.sub(r"[_-]+", " ", re.sub(r"\.gcode\.3mf$", "", file, flags=re.I))
        mname = machine.spec.get("name") or machine.id
        if self.require_confirmation:
            outcome = f"The {thing} is ready to start on {mname}. Press Start on the Overview to begin."
        else:
            self.confirm_job(job["id"])
            who = self.robots[robot_id]["name"] if robot_id else "a robot"
            outcome = f"Starting the {thing} on {mname}. When it's done, {who} will bring it to {to_zone}."
        return self._join(said, outcome + self._robot_warning())

    def _chat_start_run(self, text: str, choice: dict, document, said: str) -> str:
        if not self.machines:
            return "There are no machines yet. Add one in **Settings → Machines**, then ask me again."
        name = (choice.get("product") or "").strip()[:60] or "Production"
        name = re.sub(r"[^\w .,()/+#-]", " ", name).strip() or "Production"
        quantity = int(choice.get("quantity") or 0)
        if quantity < 1:
            return "How many units should I make?"
        if any(r["status"] in {"running", "paused"} for r in self.runs.values()):
            return "A production run is already active. Stop it in the Production tab first, or wait until it completes."
        try:  # start_run takes the lock itself, and may ask the model which parts make the product
            run = self.start_run({"name": name, "target": quantity, "description": text,
                                  "document_id": document["id"] if document else None,
                                  "approve_each_job": self.require_confirmation})
        except (ValueError, KeyError) as e:
            return f"I couldn't start production: {e}."
        with self.lock:
            self.runs[run["id"]]["origin"] = "chat"
            warning = self._robot_warning()
        parts = ", ".join(f"{q}× {f}" for f, q in run["parts"].items())
        return self._join(said, f"Production run {run['id']} started: {quantity} × {name} ({parts} per unit). "
                                "Follow it in the Production tab." + warning)

    # ------------------------------------------------------------------ printing an attached model

    def _printers(self, machine_id: str | None = None) -> list:
        printers = [m for m in self.machines.values() if m.type == "3d_printer"]
        if machine_id:
            printers = [m for m in printers if m.id == machine_id] or printers
        return sorted(printers, key=lambda m: (not is_live(m), m.id))

    def _printer_line(self, m) -> tuple[str, bool]:
        """Plain-language state of a printer, and whether it can start a print now."""
        name = m.spec.get("name") or m.id
        info, state = m.describe(), m.state()
        kind = "connected" if is_live(m) else "simulated"
        if state == "offline":
            err = info.get("error")
            return f"**{name}** is offline: it is not responding{f' ({err})' if err else ''}. Check it is on and on the same network.", False
        if state == "error":
            return f"**{name}** reports an error. Check the printer screen.", False
        if state == "busy":
            pct = info.get("progress_pct")
            what = re.sub(r"\.gcode\.3mf$", "", info.get("job") or "a part", flags=re.I).replace("_", " ")
            return f"**{name}** ({kind}) is busy printing {what}{f' ({pct}%)' if pct is not None else ''}.", False
        if self._machine_holding(m.id):
            return f"**{name}** ({kind}) has a finished part on the bed, waiting for a robot to collect it.", False
        return f"**{name}** ({kind}) is idle and ready.", True

    def _printer_report(self, ask_for_file: bool, thing: str = "") -> str:
        lines = [self._printer_line(m)[0] for m in self._printers()]
        if not lines:
            return "There is no 3D printer yet. Add one in **Settings → Machines**."
        ready = any(self._printer_line(m)[1] for m in self._printers())
        text = "\n".join(lines)
        if ask_for_file:
            what = f" for the {thing}" if thing else ""
            text += (f"\n\nI don't have a print file{what} yet. Attach the sliced model (.gcode.3mf) with 📎 and I'll "
                     + ("print it right away." if ready else "print it as soon as a printer is free."))
        return text

    @staticmethod
    def _thing(text: str) -> str:
        m = re.search(r"\bprint(?:\s+(?:me|out|off))?\s+(?:a|an|the|some|my|this)?\s*([\w -]{2,40})", text, re.I)
        return m.group(1).strip().rstrip(".!?") if m else ""

    def _chat_print_attached(self, file: str, machine_id: str | None, robot_id: str | None) -> str:
        printers = [m for m in self._printers(machine_id) if file in m.list_files()]
        if not printers:
            return f"I couldn't find {file} on a printer. Please attach it again."
        free = [m for m in printers if self._printer_line(m)[1]]
        if not free:
            return "\n".join(self._printer_line(m)[0] for m in printers) + \
                f"\n\n{file} is saved on the printer. Ask me to print it again when the printer is free."
        m = free[0]
        try:
            job = self.print_file(m.id, file, robot_id)
        except (ValueError, KeyError) as e:
            return f"I couldn't start the print: {e}"
        with self.lock:
            self.jobs[job["id"]]["origin"] = "chat"
        name = m.spec.get("name") or m.id
        thing = re.sub(r"\.gcode\.3mf$", "", file, flags=re.I).replace("_", " ")
        if job["status"] == "awaiting_confirmation":
            return f"{name} is connected and ready. The print of {thing} is waiting for your approval (Activity → Jobs)."
        return f"{name} is connected and ready. Printing **{thing}** now. When it's done, a robot will collect it."

    # ------------------------------------------------------------------ helpers

    def _machine_holding(self, machine_id: str) -> bool:
        return any(j["machine_id"] == machine_id and j["status"] in {"awaiting_confirmation", "starting", "running", "done"}
                   for j in self.jobs.values())

    def _robot_warning(self) -> str:
        online = [r for r in self.robots.values() if r["status"] not in {"offline", "fault"}]
        if not self.robots:
            return " ⚠ There are no robots yet, so nobody can collect the part. Add one in Settings → Robots."
        if not online:
            return " ⚠ No robots are online right now. Add or connect a robot in Settings so the part can be collected."
        return ""

    def _status_summary(self) -> str:
        online = sum(r["status"] in {"idle", "busy", "charging"} for r in self.robots.values())
        busy_m = sum(m.state() == "busy" for m in self.machines.values())
        run = next((self.run_view(r) for r in self.runs.values() if r["status"] in {"running", "paused"}), None)
        text = (f"{len(self.machines)} machine(s), {busy_m} running. {online} of {len(self.robots)} robots online. "
                f"{sum(t['status'] == 'pending' for t in self.tasks.values())} task(s) waiting for a robot.")
        if run:
            text += f" Production {run['id']} ({run['name']}): {run['units']}/{run['target']} units, {run['status']}."
        return text

    @staticmethod
    def _join(said: str, outcome: str) -> str:
        return f"{said}\n\n{outcome}" if said else outcome
