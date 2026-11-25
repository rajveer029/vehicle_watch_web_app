# VehicleWatch web app

Local prototype based on `Development Blueprint.docx`. It includes citizen registration, stolen vehicle and complaint reporting, evidence upload, officer verification, precomputed JSON import, active profile reconciliation, alert review, officer-approved citizen notifications, and an audit log. Data persists in SQLite under `data/`.

## Run

Requires Python 3.10 or later. From this folder:

```powershell
python server.py
```

Open http://127.0.0.1:8000. The demo officer account is `officer@vehiclewatch.local` with password `OfficerDemo123!`. Create a citizen account in the app.

## Demo flow

1. Register a citizen, sign in, and report a black Mahindra XUV700 with registration `JK02AB1234`. Any theft date and location can be used for this demo; complete the required details.
2. Sign in as the demo officer and verify the report.
3. In the officer portal, open **Video repository → RTO registry** and click **Load sample registry**, or upload your own CSV/JSON file. Then click **Load sample log**. It creates three alerts for the verified black XUV700: an exact stolen plate (**Highest urgency**), a plate registered to a white XUV700 (**High risk**), and a plate registered to a matching black XUV700 (**Low risk**).
4. Review an alert, confirm it, then approve the citizen notification.
5. Sign back in as the citizen to see the update.

For the presentation upload flow, open **Video repository → Upload a video for analysis** and upload a file named `cctv1.mp4`. You can download the bundled [sample input video](web/cctv1.mp4) from that card. The app immediately selects [cctv1.json](demo_backend/cctv1.json) and [cctv1inference.mp4](demo_backend/cctv1inference.mp4), stores the uploaded MP4 as original footage, and opens the result in Video packages. This is a filename-based demo lookup; the uploaded content is not processed by AI. Verify the vehicle report before uploading to see alerts immediately, or verify it afterward to reconcile the stored detections.

To add another demo package, place `<name>.json` and `<name>inference.mp4` in `demo_backend/`, then upload `<name>.mp4` from the officer page. The JSON needs a valid `video_id` and `detections` list. Unknown filenames show a missing-package message.

The sample video contains three black XUV700 detections. `JK02AB1234` is the exact stolen plate and creates the **Highest urgency** alert. `JK02ZZ9988` belongs to a white XUV700 in the sample registry and creates the **High risk** plate mismatch. `JK02EF4321` belongs to a black XUV700 and creates the **Low risk** alert. The alert queue shows the detected plate and lookup reason. For a new report, use the same make, model and colour; the system matches these attributes rather than requiring a particular profile ID.

## Delete a test citizen

Sign in as the demo officer, open **Overview**, and use **Delete a test citizen and their data**. Enter the citizen's exact email and type `DELETE`. This permanently removes that citizen account, complaints, vehicle reports, related alerts and notifications, and uploaded evidence. Shared video logs and synthetic RTO records remain available for further demos. The officer account cannot be deleted from this control.

You may import your own inference JSON matching the blueprint's `video_id`, `video_metadata`, and `detections` shape. Each detection should include observed `vehicle` make/model/colour, OCR `registration.text`, and `registration_comparison` with `lookup_status` and `registered_vehicle`. The app compares all visually matching detections against active verified reports; it does not require a preassigned candidate profile ID. Unreadable plates and incomplete/failed lookups are marked **Needs lookup**, never low risk. The exact-plate category is still queued for officer review before any citizen notification. The demo video upload accepts MP4s up to 250 MB. The separate manual attach control in video details accepts MP4s up to 10 MB. Video inference itself is deliberately offline, as specified by the blueprint.

## Synthetic RTO registry

Sign in as the officer, open **Video repository**, scroll to **RTO registry**, choose a `.csv` or `.json` file, then click **Import registry**. The provided [sample-rto.csv](web/sample-rto.csv) shows the required CSV columns:

```csv
plate,manufacturer,model,colour
JK02ZZ9988,Mahindra,XUV700,white
JK02AB1234,Mahindra,XUV700,black
JK02EF4321,Mahindra,XUV700,black
```

JSON may be an array of objects with the same four fields, or an object with a `vehicles` array. Imports add new plates and update existing plates. The registry is stored in `data/app.db`; importing immediately rechecks existing video alerts. Plate spacing and punctuation are ignored during lookup. Once the registry contains records, a plate absent from it is **Needs lookup**. With an empty registry, the app uses lookup results embedded in inference JSON.

## Scope

This is a local demonstration app. It uses your synthetic registry or precomputed registration lookup results from JSON; it does not connect to a live RTO database. A live RTO integration requires an authorised API and credentials. It has one seeded officer account, basic session tokens, and no production deployment, email/SMS service, or live AI processing. Do not use it for real citizen or law enforcement data without a security and privacy review.
