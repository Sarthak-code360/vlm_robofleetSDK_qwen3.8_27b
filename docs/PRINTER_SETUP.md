# Printer setup: Bambu Lab over LAN

robofleet uploads a sliced file to a Bambu Lab printer over FTPS and starts it over MQTT, both on the local network.
It has been tested on an **A1**. Other Bambu printers (A1 mini, P1P, P1S, X1C) use the same protocol.

## Why LAN mode

Since early 2025, Bambu firmware only accepts print commands from Bambu's own apps over the cloud. Third-party
tools such as robofleet need **LAN Only Mode** and **Developer Mode**. While LAN Only Mode is on:

- people on the same network can still print from Bambu Studio,
- cloud printing from outside the network, the Bambu Handy app and remote camera stop working,
- turning LAN Only Mode off restores everything.

If the printer is shared, agree with the other users before you switch.

## One-time setup

1. **Printer screen:** go to Settings > LAN Only Mode and turn it **on**. Then turn **Developer Mode** on.
   The screen now shows the **access code**. Note the **IP address** (Settings > WLAN) and the **serial number**
   (Settings > Device).
2. **microSD card** in the printer. Uploaded files are stored on it.
3. **Same network:** the computer running robofleet must be on the printer's network. The A1 uses 2.4 GHz Wi-Fi only.
4. **Install:**
   ```sh
   pip install "robofleet[printer]"
   ```
5. **Config file:** copy [`examples/printer.example.json`](../examples/printer.example.json) to `config/printer.json`
   and fill in `ip`, `serial` and `access_code`. This file holds a secret. It is git-ignored, so never commit it.
6. **Check:**
   ```sh
   robofleet printer status        # "gcode_state": "IDLE"
   ```

## Prepare files to print

The model chooses between files you sliced in advance, which is fast and reliable.

1. In Bambu Studio, select your printer (e.g. **Bambu Lab A1 0.4 nozzle**) and the loaded filament.
   You do not need to log in.
2. Add or open a model. Downloaded `.3mf` files must be sliced before printing.
3. Keep everything on **plate 1**, using **one filament** (robofleet prints without the AMS unless `use_ams` is true).
4. Click **Slice plate**, then **File > Export > Export plate sliced file**.
5. Save it into `prints/` with a short name and no spaces, e.g. `prints/cube.gcode.3mf`.

Do **not** press Print in Bambu Studio. That bypasses robofleet and may send the job to another printer on your account.

## Print

```sh
export VLM_BASE_URL=http://localhost:8000/v1 VLM_MODEL=qwen VLM_THINKING=off   # see MODEL_SETUP.md
robofleet printer list                              # files the model can choose from
robofleet printer ask "print the cube" --dry-run    # only show the model's choice
robofleet printer ask "print the cube"              # check the bed is clear, type PRINT
robofleet printer status                            # PREPARE / RUNNING / FINISH
```

The model can only pick a file that exists in `prints/`. The printer must be idle, and a person must type `PRINT`
unless `--yes` is given. Keep that confirmation whenever nobody can see the printer.

## Remote printing

To print from outside the office, put an always-on computer on the printer's network and join it to a private VPN
such as [Tailscale](https://tailscale.com) as a subnet router (`tailscale up --advertise-routes=192.168.1.0/24`).
Machines on the same tailnet can then reach the printer's IP as if they were local. Do not expose the printer's ports
(990, 8883) to the internet.

## Troubleshooting

| Problem | Fix |
|---|---|
| `No route to host` / `Host is down` | Restart the printer, then confirm `ping <printer-ip>` works. Check both devices are on the same network and the printer's IP has not changed. On macOS, allow Terminal under System Settings > Privacy & Security > Local Network |
| `Printer did not answer` (timeout) | Wrong access code, or Developer Mode is off |
| Upload fails | No microSD card, or LAN Only / Developer Mode is off |
| Printer shows "locked" in Bambu Studio | Expected in LAN Only Mode. robofleet talks to the printer directly |
| Filament / AMS mismatch on the printer | Set `"use_ams": true` in `config/printer.json`, or slice with the loaded filament |
| Prints the wrong plate or nothing | Export from plate 1 |

## Security notes

The printer uses a self-signed certificate, so robofleet does not verify it. Only use this on a network you trust.
Anyone with the access code can control the printer, so treat it like a password.

## How robofleet talks to the printer

Everything happens on your local network; nothing goes through Bambu's cloud. The printer must be in
**LAN Only + Developer Mode**, and every connection logs in as user `bblp` with the printer's **access code**.

| What | Port / protocol | Used for |
|---|---|---|
| MQTT over TLS | 8883 | Status (state, %, time left, temperatures) every few seconds, and the "start print" command |
| FTPS (implicit TLS) | 990 | Uploading the sliced `.gcode.3mf` onto the printer's microSD card |
| Camera stream over TLS | 6000 | Live JPEG pictures (A1 / A1 mini / P1) |

A print from the dashboard: the file is uploaded with FTPS → an MQTT `project_file` command tells the printer to
print it from the SD card → the factory reads MQTT status until the printer reports `FINISH` → a robot task is
created to collect the part.

The printer uses a self-signed certificate, so certificate checks are off. Use this only on a trusted network.
If the camera shows "not reachable", look for a **LAN Only Liveview** setting on the printer and turn it on.
