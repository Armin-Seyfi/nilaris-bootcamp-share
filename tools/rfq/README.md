# Local pump Schema review

The Reviewer builds a Product Type Schema draft from the pinned pump table in `Case_003/2.pdf`. Supply the second pinned PDF, `Case_003/1.pdf`, to compare its seventh note with row 5 of the table. On each launch, PDFKit reads the original bytes, locates the numbered rows in the **PUMP TECHNICAL DATA** section on page 1, and extracts the label, value and unit cells. The local draft uses the original qualified label as the proposed Attribute name. An empty observed value stays empty.

1. From the candidate checkout on macOS, prepare a fresh private bundle. Supply both PDFs in the order shown and a new destination outside the repository. The preparer verifies each revision. For the single-document draft, omit the second PDF.

   ```sh
   swift tools/rfq/prepare.swift "/path/to/Case_003/2.pdf" "/path/to/Case_003/1.pdf" "/path/to/new-private-bundle"
   ```

2. Run `tools/rfq/reviewer "/path/to/new-private-bundle"`. Open **http://127.0.0.1:18768/** and enter the bundle’s `reviewer.key` in **Local Reviewer key**. The launcher denies outbound connections.
3. In **Product Type Schema draft**, inspect the reported coverage. The section contains 20 numbered rows, with 14 proposals and 6 unresolved. The cross-document proposal adds one observation from note 7 in the second revision.
4. Compare **Flow-rated** and **Flow-normal**. For either proposal, select **View row** to inspect the original text, page highlight and SHA-256. Select **Open original PDF at row** to inspect the same location in Chrome.
5. Inspect row 12, which has a printed number and no readable label. Row 20 spans nested cells. Both have an **Unresolved** outcome, a reason and an evidence link. Neither creates an Attribute.

## Cross-document Alias review

1. In **Product Type Alias review**, read the initial proposal, the original text of page 1, row 5 and the full text of page 1, note 7. The second PDF identifies the subject of the note as **Pump design pressure(7)**.
2. Choose **View original row** and **View original note**. Each evidence view shows its revision digest and highlights the passage on page 1. The **Open original PDF** link opens the pinned bytes at that location.
3. The technical Reviewer edits the Attribute name if needed, selects **Confirm Product Type Alias** or **Keep definitions separate**, and chooses **Save Alias review**. The decision starts empty. Refresh to reinspect the saved name and decision alongside the initial proposal and both original observations. Edits last for the local session.

## Recorded test responses

Expand **Recorded test responses** below the draft to open the older local fixtures. They retain their request captures, verdicts and evidence locations.

1. Choose **Rated selection** or **Normal selection**. Before editing, expand **Historical evidence**. **View highlighted original page** opens the selected row on the rendered page. **Open original PDF at row** opens the original in Chrome at 200% zoom with the selected row immediately below the PDF toolbar.
2. **Inspect minimized request** shows the exact headers and serialized body, the recorded test response, and the local transport receipt for that run. The two qualified labels and the unit are available to the discovery task. Either label can be selected by a response.
3. Choose **Missing qualifier** to see the initial proposal *Flow* fail. Choose **Wrong row** to see *Rated flow rate* fail with row 2 selected. Edit the name and press **Save session edit**. The initial name, evidence and verdict remain visible alongside the edited name.

| Local test response | Initial name | Selected evidence | Initial verdict |
|---|---|---|---|
| Rated selection | Rated flow rate | Row 1, Flow-rated | Passed |
| Normal selection | Normal flow rate | Row 2, Flow-normal | Passed |
| Missing qualifier | Flow | Row 1, Flow-rated | Failed · missing qualifier |
| Wrong row | Rated flow rate | Row 2, Flow-normal | Failed · wrong row |

All four runs use an in-memory transport and zero external model requests. Each run and its edits last for the local session. Press Control-C to close the Reviewer, or allow the two-hour session to expire.

## Approve and reopen an exact pump Schema version

1. Prepare the two-PDF bundle as above. Start the guarded local PostgreSQL service for the review database. The application role needs the existing NILARIS schema and application grants. Keep its configuration in an owner-only (mode `0600`) JSON file outside the checkout. The `database` object contains `host` (`127.0.0.1`), `port`, `dbname`, `user` and `password`. The top-level `monitor_path` points to the guard’s local service monitor.
2. Run `tools/rfq/reviewer "/path/to/private-bundle" "/absolute/path/to/review-config.json"`. Open **http://127.0.0.1:18776/** and authenticate with the key from the bundle. The configured launcher permits a TCP connection to the configured loopback PostgreSQL port. The Reviewer session lasts up to 15 minutes.
3. Open **View original row** and **View original note**. Review the Attribute name and Alias decision, then choose **Save Alias review**. Inspect the draft revision, the correction history and **Schema definitions for approval**, including the types, units, origins and source observations.
4. Choose **Approve entire Schema draft**. The read-only version opens with its version number and ID. Bookmark this URL. PostgreSQL retains the approval. Keep the original two PDFs in the bundle for the evidence links.
5. Open the bookmark in a fresh browser session and authenticate. If the service window has ended, restart the guard on the same database and relaunch the Reviewer with the same inputs. Use the newly generated local Reviewer key.

## Bootcamp presentation

Open the [presented walkthrough](../../presentation/NILARIS-prototype-walkthrough.html) in Chrome and select **Start walkthrough**. The [storyboard](../../presentation/storyboard.csv) lists each scene. This snapshot omits the historical Case_001 source documents and disables their local Reviewer and import commands.
