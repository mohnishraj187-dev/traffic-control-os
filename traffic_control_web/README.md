# TrafficControl OS Web Backend

This is a dependency-free Python web app for the login page, public portal, and admin portal.

## Run

```powershell
cd "C:\Users\mohni\OneDrive\Documents\New project"
python traffic_control_web\app.py
```

Open:

- Login: `http://127.0.0.1:5000/`
- Public portal: `http://127.0.0.1:5000/public`
- Admin portal redirect: `http://127.0.0.1:5000/admin-portal`
- Admin dashboard: `http://127.0.0.1:5000/admin/dashboard`

The app binds to `0.0.0.0` by default so it can run on public hosting services. For local use, still open `http://127.0.0.1:5000/`.

## Google Sign-In

For real Google account login, create OAuth credentials in Google Cloud Console and set these environment variables before starting:

```powershell
$env:GOOGLE_CLIENT_ID="your-client-id"
$env:GOOGLE_CLIENT_SECRET="your-client-secret"
$env:APP_BASE_URL="http://127.0.0.1:5000"
python traffic_control_web\app.py
```

Add this redirect URI in Google Cloud Console:

```text
http://127.0.0.1:5000/auth/google/callback
```

If credentials are not set, the Google buttons use local demo sessions so the full portal flow still works.

Admin portal access is restricted to:

```text
mohnishraj187@gmail.com
```

Any other Google account that tries the admin login is redirected to an access denied page.

## Google Maps Traffic

For live Google Maps traffic visualization, enable the Maps JavaScript API in Google Cloud Console and set:

```powershell
$env:GOOGLE_MAPS_API_KEY="your-maps-api-key"
python traffic_control_web\app.py
```

The public portal uses Google Maps JavaScript `TrafficLayer` when the key is present. Without the key, it shows a local fallback traffic preview so the buttons and dashboard still work.

## Flow

- Public QR scan posts to `/api/qr-scan`; the admin dashboard shows the scan count.
- Public "Start Scan" opens the camera using `getUserMedia`; automatic QR decoding works in browsers that support `BarcodeDetector`.
- The public portal generates a scannable QR that opens `/qr-direct`; opening/scanning that URL sends a request straight to admin.
- Public traffic buttons work: Best Route asks for a destination and uses Google Maps Directions with traffic-aware driving time when `GOOGLE_MAPS_API_KEY` is set; Layers toggles map overlays, zoom controls change map zoom, locate centers on the current device location, and refresh reloads the traffic summary.
- Public QR scans send the scanned code plus the scanner's current location to admin.
- Public accident reports try to read GPS coordinates embedded in the uploaded photo, then fall back to the device location field; the admin dashboard shows that location in the Accident Section.
- Admin QR and accident cards show where the request came from and include a Control button that selects that place before manual signal override.
- Admin manual buttons post to `/api/signal`; the page updates the current signal state.
- Admin control toggles post to `/api/control-toggle`.
