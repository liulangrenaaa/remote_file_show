# Remote File Show

Restricted text file browser and editor served behind a reverse proxy. It supports file browsing, UTF-8 text editing, Git commit inspection, multi-commit copy, line numbers, and lightweight syntax highlighting for Git diffs, C-family files, and text logs.

## Deploy

1. Copy `app.py` and `workspace-file-portal.service` to the target host.
2. Edit the service file for the target account, source directory, and allowed roots. The defaults are `/tmp` and `/home/ubuntu/workspace`.
3. Install and start the service:

```bash
sudo install -m 0644 workspace-file-portal.service /etc/systemd/system/workspace-file-portal.service
sudo systemctl daemon-reload
sudo systemctl enable --now workspace-file-portal.service
```

The service binds to `127.0.0.1:7683` and serves `/shell_file/`. Configure the target host's Nginx and FRP independently. Their credentials and tokens are intentionally not stored in this repository.

## Security

Only the directories listed in `FILE_PORTAL_ROOTS` are accessible. Keep the service behind authenticated Nginx, set the allowed roots narrowly, and do not commit FRP, Nginx, or authentication secrets.
