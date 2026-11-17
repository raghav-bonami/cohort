## Schema & Staleness Design

### The document
(paste the field list from app/domain/documents.py, one line per field)

### How a reader knows which content produced the result
- `content_version` starts at 1 and every PATCH increments it.
- `result` carries its own `content_version` and `content_hash`.
- `GET` returns both numbers plus `result_is_current = (result.content_version == content_version)`.
  It's a comparison of two stored values, not a flag someone must remember to update.

### Why a mixed summary/tags result is impossible (not just unlikely)
1. **Written together.** Summary and tags live in one subdocument and are written in one `$set`.
   A single-document write in MongoDB is atomic: a reader sees all of it or none of it.
2. **Fenced writes.** Every worker write filters on `{_id, content_version, lease_id}`.
   If a PATCH bumped the version mid-run, the write matches nothing and is discarded.
3. **Tags only from the matching summary.** Stage 2 only uses `stage1_output` whose
   `content_version` equals the document's current one.

### Walkthrough: PATCH arrives while enriching is running
(step by step: worker holds v2 → PATCH sets v3, clears lease_id, resets stages →
worker finishes, tries to write with content_version=2 → 0 matched → discarded and logged →
reader meanwhile sees result v2 with result_is_current=false → new run publishes v3)
