# Rainette Music iPhone PWA

This directory is a static Progressive Web App intended to be deployed as a separate Vercel project with `pwa/` selected as the project root.

## Architecture

```text
iPhone Safari / Home Screen
        ↓ loads static files once
Rainette PWA hosted by Vercel
        ↓ authenticated HTTPS requests
Trusted tunnel to the user's computer
        ↓ loopback only
pwa_companion.py on Windows or macOS
        ↓
Rainette music_bridge → yt-dlp / YouTube Music
```

Vercel does **not** run yt-dlp, store the user's access key, proxy the audio, or need a long-running backend. After the PWA loads, Safari talks directly to the user's HTTPS companion endpoint. Audio is resolved and relayed by the user's computer through short-lived random grant URLs.

The computer must remain on and `pwa_companion.py` plus the HTTPS tunnel must remain running. A Vercel deployment alone cannot reach a computer behind a home router.

## 1. Deploy the PWA to Vercel

Create a Vercel project from this repository and set the **Root Directory** to `pwa`.

No build command or environment variables are required. The output is static.

Assume the resulting URL is:

```text
https://your-rainette-pwa.vercel.app
```

## 2. Create a trusted HTTPS endpoint for the computer

The companion intentionally listens on `127.0.0.1:47888`. Put a trusted HTTPS tunnel in front of it. A stable named tunnel is preferable because the pairing stays valid after restarts.

Example Cloudflare Tunnel ingress target:

```text
http://127.0.0.1:47888
```

Assume the public tunnel URL is:

```text
https://music-pc.example.com
```

Do not expose port `47888` directly through router port forwarding. The loopback listener plus a managed HTTPS tunnel is safer and avoids browser certificate errors.

## 3. Start the companion on the computer

From the repository root:

```bash
pip install -r requirements.txt
python pwa_companion.py \
  --pwa-url https://your-rainette-pwa.vercel.app \
  --public-url https://music-pc.example.com
```

Windows PowerShell uses backticks instead of backslashes for multiline commands, or put the command on one line.

The companion:

- uses the same Rainette database and `music_bridge` handlers;
- runs search and stream resolution with the computer's installed `yt-dlp`;
- binds only to loopback by default;
- generates a persistent 384-bit access key in Rainette's application-data directory;
- prints a pairing link and an ASCII QR code;
- permits only the exact PWA origin and Rainette's existing mobile music-command allowlist;
- replaces upstream media URLs with expiring PC relay grants.

Additional exact origins can be allowed explicitly:

```bash
python pwa_companion.py \
  --pwa-url https://your-rainette-pwa.vercel.app \
  --public-url https://music-pc.example.com \
  --origin https://rainette-preview.example.com
```

## 4. Pair the iPhone

Open the printed pairing link on the iPhone or scan the QR code. The endpoint and access key are stored by the PWA and removed from the address bar immediately.

The access key is placed after `#` in the pairing URL. URL fragments are processed by the browser and are not sent to Vercel in the HTTP request.

In Safari, tap **Share → Add to Home Screen**.

## Security boundaries

- The PWA accepts only trusted HTTPS companion endpoints, except localhost during desktop testing.
- The gateway requires a bearer token for status, commands, and event polling.
- Cross-origin requests are accepted only from exact configured origins.
- Arbitrary desktop commands are rejected; the gateway reuses Rainette's mobile music-command allowlist.
- Raw YouTube/Google media URLs are not returned to the PWA. The computer issues random, expiring relay grants and supports HTTP Range requests for seeking.
- The token is persistent until its local token file is deleted. Disconnecting inside the PWA removes only that iPhone's saved copy.

## Current scope

This is a browser companion path, isolated from the existing Android Capacitor companion so Android certificate pinning and native media controls are not changed.

The PWA supports search, library sync, local iPhone playback, lock-screen Media Session controls, recent history, reconnection polling, and an offline application shell. Music itself still requires the computer and internet connection.
