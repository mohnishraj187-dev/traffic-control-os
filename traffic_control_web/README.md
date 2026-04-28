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
garvnijhawan24@gmail.com
```

You can override this on Render with:

```text
ADMIN_EMAILS=mohnishraj187@gmail.com,garvnijhawan24@gmail.com
```

Any other Google account that tries the admin login is redirected to an access denied page.

## Free Map And Routing

The public portal does not require a Google Maps API key. It uses:

- Leaflet for the browser map
- OpenStreetMap tiles for the map view
- Built-in local destination matching for common demo places
- Nominatim as a fallback to find other typed India destinations, with query variants and local caching
- Real road route lines from the user's current location using OSRM, with successful routes cached locally

The route API is exposed at `/api/route` and accepts `origin_lat`, `origin_lng`, and `destination` query parameters.

## Flow

- Public QR scan posts to `/api/qr-scan`; the admin dashboard shows the scan count.
- Public "Start Scan" opens the camera using `getUserMedia`; automatic QR decoding works in browsers that support `BarcodeDetector`.
- The public portal generates a scannable QR that opens `/qr-direct`; opening/scanning that URL sends a request straight to admin.
- Public traffic buttons work: Show Route asks for a destination and draws a road route from the current location; Map Style toggles map layers, zoom controls change map zoom, locate centers on the current device location, and refresh reloads the traffic summary.
- Public QR scans send the scanned code plus the scanner's current location to admin.
- Public accident reports try to read GPS coordinates embedded in the uploaded photo, then fall back to the device location field; the admin dashboard shows that location in the Accident Section.
- Admin QR and accident cards show where the request came from and include a Control button that selects that place before manual signal override.
- Admin manual buttons post to `/api/signal`; the page updates the current signal state.
- Admin control toggles post to `/api/control-toggle`.
- Admin AI camera control can use the browser camera with TensorFlow.js COCO-SSD vehicle detection to estimate real lane density.
- If no live camera feed is active, the admin AI panel falls back to simulated lane density for demos.
- Admin "Apply AI Signal" posts to `/api/ai-apply`, which applies the current AI recommendation to the live signal state.

## Camera AI Roadmap

The admin dashboard now includes a real browser-camera prototype. Click "Start Camera" in the AI section, allow camera permission, choose the lane direction, and point the camera at a road or traffic video. The browser detects vehicles and posts density to `/api/camera-density`.

For a production city system:

- Run a camera worker near each junction using OpenCV plus a vehicle detector such as YOLO.
- Count vehicles per lane and convert counts into density percentages.
- Post those lane densities to the backend or replace `traffic_ai_state()` with a database-backed feed.
- Keep the admin dashboard as the control room for confidence, signal recommendation, and manual override.
