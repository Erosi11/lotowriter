# Installing LotoWriter on the Linux PC (closed Wi-Fi, no internet)

Written for Ubuntu 24.04 LTS / Debian 12, including ARM boards such as the Radxa Rock 5A (Radxa OS / Debian), on a PC that sits on its own private Wi-Fi network with **no internet**. The app is served over plain HTTP at the PC's IP address, so **there are no certificates and nothing to install on phones**. Run commands as a user with `sudo`. Text in `<angle brackets>` is yours to fill in.

## What plain HTTP means in practice

- Everything works: sign-in, photos and the camera, offline checkout, review workflow.
- **One limitation:** browsers only let an app *reopen without a connection* on HTTPS. Edits made out of Wi-Fi range are still saved on the phone and upload when it's back in range, **as long as the LotoWriter page stays open**. If the browser is closed while out of range, the app opens again once the phone is back in range; nothing is lost. The app reminds users of this.
- Security comes from the Wi-Fi itself: use **WPA2/WPA3 with a strong password**, don't share it beyond the people who use the app, and keep the network separate from other plant networks.

## 1. Before you start

- [ ] The private Wi-Fi router/access point.
- [ ] A **fixed IP address** for the Linux PC, e.g. `192.168.50.10`: a DHCP reservation in the router (preferred) or a static IP on the PC.
- [ ] A **USB drive or second disk** for backups (no network shares on a closed network).
- [ ] A **UPS** for the PC.

## 2. Getting the software onto a PC with no internet

**Easiest:** connect the Linux PC to the internet **once**, just for steps 3–4 (a temporary network cable or phone hotspot), then disconnect it. Do the same for future updates.

**If it can never be connected**, prepare the files on another computer with internet, and copy them over on a USB drive:

First check what the Linux PC runs, because the downloads must match it:
```bash
uname -m                 # x86_64 = regular PC, aarch64 = ARM board (e.g. Rock 5A)
python3 --version        # e.g. 3.11 on Debian 12, 3.12 on Ubuntu 24.04
cat /etc/os-release      # OS name and version
```

1. System packages: on an internet-connected machine running **the same OS version and architecture** (for an ARM board, a second board of the same kind is easiest) run
   ```bash
   mkdir debs && cd debs
   apt-get download $(apt-cache depends --recurse --no-recommends --no-suggests --no-conflicts --no-breaks \
     --no-replaces --no-enhances python3-venv python3-pip libreoffice-writer-nogui fonts-crosextra-carlito \
     fonts-crosextra-caladea fonts-liberation fonts-dejavu-core rsync | grep "^\w" | sort -u)
   ```
   On the Linux PC: `sudo apt install ./debs/*.deb`
2. Python packages: on any computer with Python and internet, using the Python version from `python3 --version` (3.11 shown):
   ```bash
   # Regular PC (x86_64):
   pip download -r requirements.txt -d wheels --python-version 3.11 --only-binary=:all:      --platform manylinux_2_28_x86_64 --platform manylinux2014_x86_64
   # ARM board (aarch64), e.g. Radxa Rock 5A:
   pip download -r requirements.txt -d wheels --python-version 3.11 --only-binary=:all:      --platform manylinux_2_28_aarch64 --platform manylinux2014_aarch64
   ```
   If pip reports a package with "no matching distribution", tell whoever maintains the app which one.
   On the Linux PC, in step 4 use: `sudo .venv/bin/pip install --no-index --find-links /path/to/wheels -r requirements.txt`
3. The LotoWriter project folder itself (from GitHub, as a zip or `git clone`).

## 3. Install system packages (if the PC is temporarily online)

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip rsync libreoffice-writer-nogui \
  fonts-crosextra-carlito fonts-crosextra-caladea fonts-liberation fonts-dejavu-core
```

The Carlito and Liberation fonts match Calibri and Arial (used by the EHS-003a form), so PDFs lay out like Word.

## 4. App user, folders and code

```bash
sudo useradd --system --home /var/lib/lotowriter --shell /usr/sbin/nologin lotowriter
sudo mkdir -p /var/lib/lotowriter
sudo chown lotowriter:lotowriter /var/lib/lotowriter
sudo chmod 750 /var/lib/lotowriter
sudo git clone https://github.com/Erosi11/lotowriter.git /opt/lotowriter   # or copy the folder there
cd /opt/lotowriter
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt
sudo chmod +x deploy/backup.sh
```

You can disconnect the PC from the internet now.

## 5. Set the clock (important without internet)

Approval dates, review-due dates and sign-in expiry all use the PC's clock, and a PC without internet can't correct it automatically.
```bash
timedatectl                                   # check date, time and time zone
sudo timedatectl set-timezone America/New_York   # Villa Rica, GA
sudo timedatectl set-ntp false
sudo timedatectl set-time "<YYYY-MM-DD HH:MM:SS>"   # only if it's wrong
```
Check it monthly; PC clocks drift a few seconds a week. If the Wi-Fi router can act as a time server, point the PC at it instead.

## 6. Check PDF output before going live

```bash
sudo .venv/bin/python tools/make_sample_photos.py
sudo -u lotowriter env HOME=/var/lib/lotowriter LOTOWRITER_DATA=/var/lib/lotowriter \
  .venv/bin/python -m lotowriter generate samples/sample_procedure.json --out /var/lib/lotowriter/check
```

Copy `/var/lib/lotowriter/check/02-PROD-10045-V00.pdf` to a USB drive and compare it with the sample made in Word, paying particular attention to:
- the **DRAFT – NOT FOR USE** watermark
- the **Steam/Hot Water stripes** and **Start/Stop crosshatch** in the energy cells and the legend
- table rows not splitting across pages, and the header row repeating

## 7. Run the app

```bash
sudo cp deploy/lotowriter.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lotowriter
systemctl status lotowriter               # should say "active (running)"
curl -s http://127.0.0.1/api/health       # {"ok":true}
```

The service listens on **port 80** of every network interface.

### Firewall

On a closed Wi-Fi used only for this app, a firewall is extra protection rather than a requirement (the only things listening are the app and SSH). If you skip it, make sure the SSH password is strong and the board's default password has been changed.

> **Don't lock yourself out:** allow SSH from the address or subnet you actually connect from *before* turning the firewall on.

**If `ufw` is available** (Ubuntu; or `sudo apt install ufw` while online):
```bash
sudo ufw allow 80/tcp
sudo ufw allow from <admin subnet, e.g. 192.168.2.0/24> to any port 22 proto tcp
sudo ufw enable
```

**If `ufw` isn't installed and there's no internet** (e.g. Radxa OS / Debian): use nftables, which is built in (`sudo nft --version` to check).
1. Edit `deploy/nftables.conf`: change `192.168.2.0/24` to the subnet you SSH from.
2. Install and test it **without** making it permanent:
   ```bash
   sudo cp deploy/nftables.conf /etc/nftables.conf
   sudo nft -c -f /etc/nftables.conf && sudo nft -f /etc/nftables.conf
   ```
3. Open a **new** SSH session and load `http://<PC IP>` from a phone. If both work, make it permanent:
   ```bash
   sudo systemctl enable nftables
   ```
   If SSH stopped working, reboot the PC: the rules aren't permanent until this last step.

## 8. First sign-in (do this immediately)

On a phone or PC connected to the private Wi-Fi, open **`http://<PC IP>`** (e.g. `http://192.168.50.10`). Sign in as **admin** / **Password1** and set a new password straight away, **before** giving the address to anyone. Then add user accounts under **Admin**.

**On each phone/tablet:** open the address in Chrome (Android) or Safari (iPhone/iPad) and use **Add to Home screen** so users get a LotoWriter icon. Printing a sign with the address (or a QR code of it) near the work area helps.

## 9. Nightly backups (USB drive or second disk)

1. Plug in the backup drive and find it: `lsblk -f` (e.g. `/dev/sdb1`, note its UUID).
2. Mount it permanently:
   ```bash
   sudo mkdir -p /mnt/backup
   echo 'UUID=<uuid> /mnt/backup auto defaults,nofail 0 2' | sudo tee -a /etc/fstab
   sudo mount -a
   sudo mkdir -p /mnt/backup/lotowriter && sudo chown lotowriter:lotowriter /mnt/backup/lotowriter
   ```
3. Enable the nightly timer and run one backup now:
   ```bash
   sudo cp deploy/lotowriter-backup.service deploy/lotowriter-backup.timer /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now lotowriter-backup.timer
   sudo systemctl start lotowriter-backup.service
   journalctl -u lotowriter-backup -n 20      # "Backup complete: ..."
   ```

Each night creates a dated folder, keeps 30 days, and logs a warning when a disk is over 80% full. A backup drive that stays next to the PC doesn't protect against fire or theft: **once a month, copy `/mnt/backup/lotowriter/latest` to a second USB drive kept elsewhere.**

### Test a restore before the pilot

```bash
sudo systemctl stop lotowriter
sudo -u lotowriter rsync -a --delete --exclude lo-profile/ /mnt/backup/lotowriter/latest/ /var/lib/lotowriter/
sudo systemctl start lotowriter
```
Sign in and check that a procedure and its photos open.

## 10. Updating the app

Take a backup first (`sudo systemctl start lotowriter-backup.service`), then either connect the PC to the internet briefly and run
```bash
cd /opt/lotowriter && sudo git pull && sudo .venv/bin/pip install -r requirements.txt
```
or copy the new project folder in from USB (keep `.venv`). Then `sudo systemctl restart lotowriter`. Database changes are applied automatically when the app starts. After an update, users should refresh the page once.

## Troubleshooting

| Problem | Check |
|---|---|
| Sign-in seems to do nothing | The service file must contain `LOTOWRITER_SECURE_COOKIE=0` (plain HTTP). `systemctl cat lotowriter` |
| Page doesn't load on a phone | Phone is on the private Wi-Fi? `http://` not `https://`? Same IP as `hostname -I` on the PC? Firewall allows port 80 (`sudo nft list ruleset` or `sudo ufw status`)? |
| `ufw: command not found` | Not installed on Debian/Radxa OS: use the nftables option in step 7 (or `sudo apt install ufw` while online) |
| Lost the only admin password | `sudo -u lotowriter env LOTOWRITER_DATA=/var/lib/lotowriter /opt/lotowriter/.venv/bin/python -m lotowriter reset-admin`, then sign in with Password1 and change it |
| PDFs aren't created (Word file only) | `journalctl -u lotowriter -n 50`; check that `soffice --version` works |
| Dates look wrong (review due, approvals) | The PC clock: step 5 |
| A procedure is stuck "checked out" to a lost device | An Admin opens it and taps **Release (admin)**. Changes that weren't uploaded from that device are lost |

## Appendix: HTTPS (if the network later gets internet or a certificate)

HTTPS lets the app reopen with no connection at all. It needs a certificate that phones trust, either from IT or from Let's Encrypt using a domain you own (the PC needs brief internet access every 60 days to renew). Then:
1. `sudo apt install caddy`, edit and copy `deploy/Caddyfile` to `/etc/caddy/Caddyfile`, `sudo systemctl reload caddy`.
2. In `/etc/systemd/system/lotowriter.service`: remove `LOTOWRITER_SECURE_COOKIE=0`, change the ExecStart to `--host 127.0.0.1 --port 8765`, and remove the two `CAP_NET_BIND_SERVICE` lines. Then `sudo systemctl daemon-reload && sudo systemctl restart lotowriter`.
3. Firewall: allow 443 and 80 (with nftables, add `tcp dport 443 accept` to `/etc/nftables.conf`).
