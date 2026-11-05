# BUGS

Real bugs hit while building. Each entry links a `test:` commit that reproduces the bug
(`xfail(strict=True)`) and the `fix:` commit that removes the marker.

## 1. Rollback masked the real database error
- **What broke:** When a write failed in a way that makes SQLite end the transaction
  itself, `transaction()` raised `OperationalError: cannot rollback - no transaction is
  active` instead of the real error (e.g. `IntegrityError` from a trigger's
  `RAISE(ROLLBACK)`, or a disk-full / I/O error). Data was safe; the cause was lost.
- **Why:** The `except` branch always issued `ROLLBACK`. SQLite had already rolled back,
  so the second rollback failed, and Python propagated the newer exception.
- **How found:** Code review of `transaction()` while building the migration runner;
  reproduced with a `RAISE(ROLLBACK)` trigger before fixing.
- **Fix:** Roll back only if `conn.in_transaction`.
- **Regression test:** `backend/tests/unit/test_store_db.py::test_original_error_survives_when_sqlite_already_rolled_back`
- **Commits:** repro `9a809a2` · fix `0d4e6c6`

<!-- Template
## N. <short title>
- **What broke:**
- **Why:**
- **Fix:**
- **Regression test:** `path/to/test_file.py::test_name`
- **Commits:** repro `<hash>` · fix `<hash>`
-->
