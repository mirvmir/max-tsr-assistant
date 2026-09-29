# Document rendering verification

Checked 2026-09-29 using ReportLab, python-docx, Poppler and the documents skill LibreOffice renderer. All inputs are synthetic. This does not approve an official application form or a real regional procedure.

| Output | Latest pages | Result |
| --- | ---: | --- |
| purchase_card.pdf | 2 | Cyrillic, exact price, explicit unknown amounts and missing region, frozen versions, demo footer readable |
| application.pdf | 2 | Representative role, long address, unknown addressee and missing applicant explicit, unofficial model-route warning readable |
| application.docx | 2 | Same plain content as PDF; black headings, A4, no inherited title border; no clipped text |
| product_card.pdf | 2 | Price and delivery, incomplete calculation, sources and versions readable |
| checklist.pdf | 1 | Model-route warning, unknown addressee and missing region explicit |
| Long Cyrillic literal markup PDF | 1 | Repeated literal `<script>` text stays visible text and wraps cleanly |
| Long Cyrillic literal markup DOCX | 1 | Same literal text, missing applicant, hash and demo footer readable |

Latest pages were rasterized and visually inspected. An inherited blue DOCX title border was found, covered by a failing regression test, removed, and the documents re-rendered. Obsolete page PNGs from a previous longer DOCX render are not counted; the latest PDF page counts were checked with pdfinfo. QA files are local intermediates under the ignored `.superpowers/qa/task5/` directory and are not repository deliverables.

Automated checks: `python -m pytest tests/test_files.py tests/test_documents.py -q` returned **7 passed**. These cover immutable branch composition and manifest hash binding; unknown/missing values and representative role; literal Cyrillic rendering and output limit; synthetic templates rejected in pilot mode; typed validation; AES-GCM random nonces and tamper rejection; ciphertext-only storage, owner isolation, lease fence, historical acknowledgement, expiry, traversal/symlinks and orphan retention/removal.

The renderer imports no settings, ORM or cryptography and writes no plaintext files. QA writes synthetic files only. Current template assets are fixed synthetic drafts; reviewed pilot templates remain unavailable.

Environment recovery: the bundled Poppler wrapper initially failed to locate libpoppler.so.160. PDF QA used `/usr/bin/pdftoppm`; the DOCX renderer succeeded with `LD_LIBRARY_PATH=/opt/codex/runtimes/codex-primary-runtime/dependencies/native/poppler/poppler/lib`.
