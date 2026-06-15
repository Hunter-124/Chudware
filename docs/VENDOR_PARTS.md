# Vendor Parts

AI-CAD supports a supplier-part cache for purchased hardware and catalog CAD. The first backend
workflow is intentionally conservative:

- A user or approved supplier API adapter provides a CAD file.
- The worker registers supplier metadata and copies the CAD file into `%APPDATA%/AI-CAD/vendor_parts`.
- Local AI workflows can ask for compact part context without reading a catalog.
- A cached part can be inserted into a document as a normal frozen `import` feature.

## McMaster-Carr Boundary

McMaster-Carr publishes downloadable CAD models and documents an approved Product Information API
for authorized customers. Their API flow includes certificate authentication, login token exchange,
product subscriptions, CAD endpoints, and rate limits.

Do not scrape the McMaster website. Build integrations through one of these paths:

- manual user download, then `vendor_part_register_local_cad`;
- an approved API adapter that authenticates with customer credentials/certificates and writes the
  same vendor-part records;
- a commercial CAD add-in/import handoff where the user explicitly exports a supported file.

Useful official references:

- https://www.mcmaster.com/cad-models/
- https://www.mcmaster.com/help/api/
- https://www.mcmaster.com/solidworksaddin/

## Worker Methods

`vendor_part_register_local_cad`

Registers a CAD file and supplier metadata. Supported file extensions are `.step`, `.stp`, `.igs`,
`.iges`, `.brep`, and `.stl`.

`vendor_part_context`

Returns a compact `llm_context` list and capability reminders. Use this before asking a local model
to design around off-the-shelf parts.

`vendor_part_insert`

Adds an `import` feature to an existing part document, or creates a new part document when `doc_id`
is omitted. The feature params preserve supplier, part number, URL, source path, and detected CAD
format so the history tree remains auditable.

## Local AI Workflow

For limited local models:

1. Search/list registered vendor parts with a narrow query.
2. Inject only `vendor_part_context`, not the full index.
3. Ask the model to choose part numbers explicitly.
4. Insert CAD with `vendor_part_insert`.
5. Design references, fixtures, clearances, and enclosures around the imported feature ids.

This keeps the purchased part as a human-readable, orderable anchor in the design history.
