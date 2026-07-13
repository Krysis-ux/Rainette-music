# Rainette Desktop Mobile Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a desktop Mobile page that explains Android installation, links to a signed GitHub Release APK, and securely creates/approves/revokes same-Wi-Fi phone pairings.

**Architecture:** A focused `rainette_mobile.js` module renders the page and talks only to methods exposed by `WindowApi`. Python owns pairing secrets, QR generation, TLS identity, and device authorization. GitHub Actions builds and signs the Capacitor APK on version tags and publishes the exact filename used by the desktop download link.

**Tech Stack:** Python 3.12, pywebview, qrcode/Pillow, aiohttp, vanilla JavaScript/CSS, Capacitor 7, Android Gradle, GitHub Actions.

## Global Constraints

- Download URL is exactly `https://github.com/Krysis-ux/Rainette-music/releases/latest/download/rainette-music-android.apk`.
- Install and pairing use separate QR codes; pairing URIs use the `rainette://pair` scheme.
- Pairing invitations expire after 300 seconds and require desktop approval.
- QR payloads never include `APP_TOKEN` or permanent device credentials.
- Android signing secrets are `ANDROID_KEYSTORE_BASE64`, `ANDROID_KEYSTORE_PASSWORD`, `ANDROID_KEY_ALIAS`, and `ANDROID_KEY_PASSWORD`.
- Never publish an unsigned APK as the production download.

---

### Task 1: Pairing management state

**Files:**
- Modify: `companion.py`
- Modify: `server.py`
- Test: `tests/test_companion.py`

**Interfaces:**
- Produces: `CompanionRegistry.pending_requests() -> list[dict]`, `devices() -> list[dict]`, `reject(request_id) -> bool`.
- Produces: `server.companion_management_state() -> dict`, `server.reject_companion_request(request_id) -> bool`.

- [ ] **Step 1: Add failing registry tests**

```python
def test_pending_requests_can_be_rejected(self):
    registry = CompanionRegistry(now=lambda: 1000)
    invite = registry.create_invitation(ttl_s=300)
    pending = registry.request_pairing(invite["token"], "Pixel", "phone-key")
    self.assertEqual(registry.pending_requests()[0]["request_id"], pending["request_id"])
    self.assertTrue(registry.reject(pending["request_id"]))
    self.assertEqual(registry.pending_requests(), [])

def test_devices_omit_credentials_and_show_revocation(self):
    device = approve_test_device(registry)
    listed = registry.devices()[0]
    self.assertNotIn("device_token", listed)
    self.assertEqual(listed["device_id"], device["device_id"])
    registry.revoke(device["device_id"])
    self.assertTrue(registry.devices()[0]["revoked"])
```

- [ ] **Step 2: Run the tests and confirm RED**

Run: `$env:PYTHONPATH=(Get-Location).Path; python -m pytest -q tests/test_companion.py`

Expected: failures because `pending_requests`, `devices`, and `reject` do not exist.

- [ ] **Step 3: Implement sanitized management methods**

Add request IDs/status timestamps to `_PairingRequest`; return only request ID, device name, and status from `pending_requests()`. Return only device ID, name, and revoked state from `devices()`. `reject()` removes a pending request without minting a credential. Add server wrappers that return `{pending, devices}`.

- [ ] **Step 4: Run focused tests and confirm GREEN**

Run: `$env:PYTHONPATH=(Get-Location).Path; python -m pytest -q tests/test_companion.py tests/test_server_artwork.py`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```powershell
git add companion.py server.py tests/test_companion.py
git commit -m "feat: expose companion pairing management"
```

### Task 2: Desktop QR and native API boundary

**Files:**
- Modify: `main.py`
- Modify: `requirements.txt`
- Test: `tests/test_window_api.py`

**Interfaces:**
- Produces: `WindowApi.companion_create_invitation()`, `companion_management_state()`, `companion_approve_request(request_id)`, `companion_reject_request(request_id)`, `companion_revoke_device(device_id)`.
- Invitation result: `{ok, pairing_uri, pairing_qr_data_url, expires_at}`.
- Produces: `WindowApi.android_download_info()` returning `{url, install_qr_data_url, published}`.

- [ ] **Step 1: Add failing WindowApi tests**

```python
def test_companion_invitation_returns_local_qr_without_launch_token(monkeypatch):
    monkeypatch.setattr(server, "create_companion_invitation", lambda: {
        "version": 1, "endpoint": "https://192.168.1.5:9999",
        "certificate_sha256": "abc", "invitation": "invite", "expires_at": 1300,
    })
    result = main.WindowApi().companion_create_invitation()
    assert result["pairing_uri"].startswith("rainette://pair?")
    assert result["pairing_qr_data_url"].startswith("data:image/png;base64,")
    assert server.APP_TOKEN not in result["pairing_uri"]
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `$env:PYTHONPATH=(Get-Location).Path; python -m pytest -q tests/test_window_api.py`

Expected: invitation result lacks `pairing_uri` and QR data.

- [ ] **Step 3: Add QR generation and API methods**

Add `qrcode[pil]>=8.0` to `requirements.txt`. Implement `_qr_data_url(value)` with `qrcode.QRCode`, `io.BytesIO`, and base64. Build the pairing URI with `urllib.parse.urlencode` from `endpoint`, `certificate_sha256`, and `invitation`. Add management/approve/reject/revoke wrappers. Generate the install QR from the exact GitHub APK URL. Determine `published` with a short background-safe HTTPS HEAD request; network failure returns `False` without blocking page rendering beyond three seconds.

- [ ] **Step 4: Run tests and confirm GREEN**

Run: `$env:PYTHONPATH=(Get-Location).Path; python -m pytest -q tests/test_window_api.py tests/test_companion.py`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```powershell
git add main.py requirements.txt tests/test_window_api.py
git commit -m "feat: expose mobile QR APIs"
```

### Task 3: Mobile desktop page

**Files:**
- Create: `web/rainette_mobile.js`
- Modify: `web/rainette_music.js`
- Modify: `web/rainette_pages.css`
- Test: `tests/test_frontend_release_contract.py`
- Test: `tests/test_browser_ui_smoke.py`

**Interfaces:**
- Consumes all `WindowApi` methods from Task 2 through `window.pywebview.api`.
- Produces: `renderMobile(host)` and `unmountMobile()`.

- [ ] **Step 1: Add failing contract/browser tests**

```python
def test_mobile_page_contract(self):
    mobile = (ROOT / "web" / "rainette_mobile.js").read_text(encoding="utf-8")
    self.assertIn("rainette-music-android.apk", mobile)
    self.assertIn("New pairing code", mobile)
    self.assertIn("Download APK", mobile)
    self.assertIn("Approve", mobile)
    self.assertIn("Reject", mobile)
    self.assertIn("Revoke", mobile)
    self.assertIn("companion_create_invitation", mobile)
```

Extend the browser smoke test to select the Mobile tab, assert the three numbered steps, verify the download anchor, and confirm the no-native pairing message at 390px width.

- [ ] **Step 2: Run tests and confirm RED**

Run: `$env:PYTHONPATH=(Get-Location).Path; python -m pytest -q tests/test_frontend_release_contract.py tests/test_browser_ui_smoke.py`

Expected: missing module/navigation/page assertions fail.

- [ ] **Step 3: Implement the focused page module**

Create `renderMobile(host)` with Download/Install/Pair cards, install QR, pairing QR, five-minute countdown, pending device actions, paired device revocation, inline status messages, and no-native fallback. Poll management state every two seconds only while mounted; `unmountMobile()` clears timers. Add `mobile` to `TAB_META` and `navItems()`, import the module, and call it from `applyTab()`. Add responsive CSS with a two-column desktop layout and stacked phone layout.

- [ ] **Step 4: Run tests and sync Capacitor assets**

Run:

```powershell
$env:PYTHONPATH=(Get-Location).Path
python -m pytest -q tests/test_frontend_release_contract.py tests/test_browser_ui_smoke.py tests/test_window_api.py
Set-Location mobile
$env:NODE_OPTIONS='--use-system-ca'
npx.cmd cap sync android
```

Expected: all tests pass and Capacitor sync completes.

- [ ] **Step 5: Commit**

```powershell
git add web/rainette_mobile.js web/rainette_music.js web/rainette_pages.css tests/test_frontend_release_contract.py tests/test_browser_ui_smoke.py mobile/android/app/src/main/assets/public
git commit -m "feat: add desktop mobile pairing page"
```

### Task 4: Signed APK release publication

**Files:**
- Create: `.github/workflows/android-release.yml`
- Modify: `mobile/android/app/build.gradle`
- Test: `tests/test_mobile_contract.py`

**Interfaces:**
- Consumes the four exact Android signing secrets in Global Constraints.
- Produces GitHub Release asset `rainette-music-android.apk` for version tags matching `v*`.

- [ ] **Step 1: Add failing workflow contract test**

```python
def test_android_release_workflow_signs_and_publishes_expected_apk():
    workflow = (ROOT / ".github/workflows/android-release.yml").read_text(encoding="utf-8")
    for secret in ("ANDROID_KEYSTORE_BASE64", "ANDROID_KEYSTORE_PASSWORD", "ANDROID_KEY_ALIAS", "ANDROID_KEY_PASSWORD"):
        assert secret in workflow
    assert "distribution: 'temurin'" in workflow
    assert "java-version: '21'" in workflow
    assert "rainette-music-android.apk" in workflow
    assert "assembleRelease" in workflow
```

- [ ] **Step 2: Run test and confirm RED**

Run: `$env:PYTHONPATH=(Get-Location).Path; python -m pytest -q tests/test_mobile_contract.py`

Expected: workflow file is missing.

- [ ] **Step 3: Implement signing and release workflow**

Configure `signingConfigs.release` from environment variables and apply it only to `buildTypes.release`. The workflow checks all four secrets before building, installs Node dependencies with `npm ci`, runs Capacitor sync, decodes the keystore into the runner temp directory, builds `assembleRelease`, copies the signed output to `rainette-music-android.apk`, and uploads it with `softprops/action-gh-release` using `contents: write`.

- [ ] **Step 4: Run verification**

Run: `$env:PYTHONPATH=(Get-Location).Path; python -m pytest -q tests/test_mobile_contract.py tests/test_frontend_release_contract.py tests/test_companion.py tests/test_window_api.py`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```powershell
git add .github/workflows/android-release.yml mobile/android/app/build.gradle tests/test_mobile_contract.py
git commit -m "ci: publish signed Android release APK"
```

## Final Verification

Run the focused Python suite, `npx cap sync android`, and an Android release build using JDK 21 and test signing values. Launch the desktop app, open Mobile, verify the missing-release state, create a pairing QR, submit a test phone request, approve it, and revoke it. Rebuild `Rainette Music.exe` only after all desktop tests pass.
