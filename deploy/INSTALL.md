# Installing LotoWriter on the Linux PC

Written for Ubuntu 24.04 LTS / Debian 12. Run the commands as a user with `sudo`. Text in `<angle brackets>` is yours to fill in.

## 1. Before you start (with IT)

- [ ] A **fixed IP address** (DHCP reservation) for the PC.
- [ ] A **DNS name**, e.g. `lotowriter.plant.local`, pointing to that address.
- [ ] A **certificate** for that name from IT's internal certificate authority (`.crt` + `.key`). If IT can't issue one, use option 6B.
- [ ] A **backup location** on another machine (network share) or a second disk.
- [ ] A **UPS** for the PC.

## 2. Install system packages

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip rsync caddy \
  libreoffice-writer-nogui \
  fonts-crosextra-carlito fonts-crosextra-caladea fonts-liberation fonts-dejavu-core
```

The Carlito and Liberation fonts have the same measurements as Calibri and Arial, which the EHS-003a form uses, so the PDF layout matches Word.

## 3. App user, folders and code

```bash
sudo useradd --system --home /var/lib/lotowriter --shell /usr/sbin/nologin lotowriter
sudo mkdir -p /opt/lotowriter /var/lib/lotowriter
sudo chown lotowriter:lotowriter /var/lib/lotowriter
sudo chmod 750 /var/lib/lotowriter
# Copy the LotoWriter project folder to /opt/lotowriter (git clone, USB copy, etc.), then:
cd /opt/lotowriter
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt
sudo chmod +x deploy/backup.sh
```

## 4. Check PDF output before going live

```bash
sudo -u lotowriter env HOME=/var/lib/lotowriter LOTOWRITER_DATA=/var/lib/lotowriter \
  .venv/bin/python tools/make_sample_photos.py
sudo -u lotowriter env HOME=/var/lib/lotowriter LOTOWRITER_DATA=/var/lib/lotowriter \
  .venv/bin/python -m lotowriter generate samples/sample_procedure.json --out /var/lib/lotowriter/check
```

Open `/var/lib/lotowriter/check/02-PROD-10045-V00.pdf` and compare it with the sample made in Word, paying particular attention to:
- the **DRAFT – NOT FOR USE** watermark
- the **Steam/Hot Water stripes** and **Start/Stop crosshatch** in the energy cells and the legend
- table rows not splitting across pages, and the header row repeating

`make_sample_photos.py` writes into the project folder. If that fails because `/opt/lotowriter` is read-only for the app user, run it once with `sudo` instead.

## 5. Run the app as a service

```bash
sudo cp deploy/lotowriter.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lotowriter
systemctl status lotowriter          # should say "active (running)"
curl -s http://127.0.0.1:8765/api/health   # {"ok":true}
```

The app only listens on the PC itself (127.0.0.1); Caddy (next step) handles HTTPS for the network.

## 6. HTTPS with Caddy

Edit `deploy/Caddyfile`, replacing `lotowriter.plant.local` with the real name, and pick a certificate option:

**6A. Certificate from IT (recommended)**
```bash
sudo mkdir -p /etc/caddy/certs
sudo cp <lotowriter.crt> <lotowriter.key> /etc/caddy/certs/
sudo chown -R caddy:caddy /etc/caddy/certs && sudo chmod 600 /etc/caddy/certs/*.key
```
If IT's root certificate isn't already trusted by plant phones/tablets (usually it is on managed devices), IT installs it.

**6B. Caddy's internal CA** (when IT can't issue a certificate)
In the Caddyfile, comment out the `tls /etc/caddy/certs/...` line and uncomment `tls internal`. After step 6 below, install this file on **every phone, tablet and PC** that uses the app:
`/var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt`
- Android: Settings → Security → Encryption & credentials → Install a certificate → CA certificate.
- iPhone/iPad: AirDrop or email the file, install the profile, then Settings → General → About → Certificate Trust Settings → turn it on.
- Windows: double-click → Install Certificate → Local Machine → Trusted Root Certification Authorities.

Then:
```bash
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile
sudo systemctl reload caddy
```
Browse to `https://lotowriter.plant.local` from a phone. There must be **no certificate warning**; if there is one, offline mode won't work on that device.

## 7. First sign-in (do this immediately)

Sign in as **admin** / **Password1** and set a new password straight away, **before** telling anyone the address. Then add user accounts under **Admin**.

## 8. Firewall

```bash
sudo ufw allow 443/tcp
sudo ufw allow 80/tcp                     # Caddy redirects to HTTPS
sudo ufw allow from <admin PC or subnet> to any port 22 proto tcp
sudo ufw enable
```
Don't expose the PC to the internet.

## 9. Nightly backups

1. Mount the backup location (e.g. a network share at `/mnt/backup`) so it mounts automatically at boot (`/etc/fstab`). Give the `lotowriter` user write access to `/mnt/backup/lotowriter`.
2. Set `BACKUP_DEST` in `deploy/lotowriter-backup.service` if it isn't `/mnt/backup/lotowriter`.
3. Enable the timer and run one backup now:
```bash
sudo cp deploy/lotowriter-backup.service deploy/lotowriter-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lotowriter-backup.timer
sudo systemctl start lotowriter-backup.service
journalctl -u lotowriter-backup -n 20      # "Backup complete: ..."
```
Each night creates a dated folder, keeps 30 days, and logs a warning when a disk is over 80% full.

### Test a restore before the pilot

```bash
sudo systemctl stop lotowriter
sudo -u lotowriter rsync -a --delete /mnt/backup/lotowriter/latest/ /var/lib/lotowriter/ --exclude lo-profile/
sudo systemctl start lotowriter
```
Sign in and check that a procedure and its photos open.

## 10. Updating the app

```bash
cd /opt/lotowriter
# copy in the new version (git pull, etc.)
sudo .venv/bin/pip install -r requirements.txt
sudo systemctl restart lotowriter
```
Database changes are applied automatically when the app starts. Take a backup first (`sudo systemctl start lotowriter-backup.service`).

## Troubleshooting

| Problem | Check |
|---|---|
| Lost the only admin password | `sudo -u lotowriter env LOTOWRITER_DATA=/var/lib/lotowriter /opt/lotowriter/.venv/bin/python -m lotowriter reset-admin`, then sign in with Password1 and change it |
| PDFs aren't created (Word file only) | `journalctl -u lotowriter -n 50`; check that `soffice --version` works |
| Phone says "Offline" on Wi-Fi | Open `https://<name>/api/health` on the phone: a certificate warning means step 6 isn't finished on that device |
| A procedure is stuck "checked out" to a lost device | An Admin opens it and taps **Release (admin)**. Changes that weren't uploaded from that device are lost |
