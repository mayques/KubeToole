# Adding a tool

Add a folder with a lowercase identifier, `tool.json`, and `tool.py`; rebuild and deploy the API and worker using the same image digest. Add dependencies to `calculator/requirements.txt` only if needed. No quota, scheduler, authentication or dashboard code changes are needed for a tool that fits this contract.

Example `tools/example/tool.json` (documentation only; not in the production catalog):

```json
{
  "name": "Example",
  "description": "Generate a sequence as CSV.",
  "execution": "job",
  "inputs": {
    "type": "object",
    "additionalProperties": false,
    "required": ["rows"],
    "properties": {
      "rows": {"type": "integer", "minimum": 1, "maximum": 100000}
    }
  }
}
```

`tools/example/tool.py`:

```python
def run(inputs, context):
    with context.output("sequence.csv") as output:
        output.write(b"index,square\n")
        for index in range(inputs["rows"]):
            output.write(f"{index},{index * index}\n".encode())
    return {"rows": inputs["rows"], "artifact_id": output.id}
```

For throughput, batch many small rows into bounded chunks before calling `write`; each call performs a reservation and a write request. Never buffer an entire large dataset just to save it.

- `run(inputs, context)` returns JSON-compatible data, at most 48 KiB. Raise an exception on failure. Do not swallow quota/storage errors and continue work.
- `execution: "job"` uses the fixed worker limits. `execution: "inline"` is for small, bounded operations only; it receives `context=None` and saves its returned JSON. The calculator preserves its existing history format.
- `context.output(name)` is an append-only binary writer. Closing its `with` block seals the file; a `.csv` output is also streamed through the shared CSV validator. A validation error fails the job. The API publishes files only after successful job termination; public downloads are limited to validated CSVs.
- `context.temporary(name)` uses the same streaming interface and the separate 5 GB temporary allowance. Once sealed, `open_reader()` returns an HTTP binary stream; `discard()` releases temporary storage early. All remaining temporary data is removed on termination.
- `context.open_input(file_id)` streams a validated upload or completed output belonging to the same account. IDs are opaque UUIDs, not filesystem paths. Uploads and outputs share the account's allowance. Use `io.TextIOWrapper(stream, encoding="utf-8-sig", newline="")` with `csv.DictReader`/`csv.reader`; stream rows, validate your expected columns and convert numeric fields explicitly. Never evaluate cell contents or load a whole 500 MB dataset by default.
- Filesystem writes are unavailable in workers. Use bounded RAM buffers or these streams. Libraries requiring a writable directory, random-access file paths, GPUs, external executables not bundled in the image, or larger limits need additional integration; declaring a tool cannot grant those capabilities.
- Do not spawn detached work, access other users' data, log large datasets, or write around the context. CPU/RAM limits cover the worker's child processes; the parent forwards cancellation and kills the process group if necessary.
- Input schemas must be self-contained JSON Schema. The generic website form supports top-level string, number, integer, boolean and enum fields; object/array fields use JSON text. More specialised interactions need a tool-specific frontend.
- For a CSV input, declare a top-level property such as `"dataset": {"type": "string", "format": "csv-file", "title": "Dataset"}` and include it in `required` when appropriate. The dashboard automatically offers that user's ready CSV uploads. Admission checks ownership/readiness before launching; pass `inputs["dataset"]` to `context.open_input`. No dashboard or quota changes are needed for the next tool using this contract.
- Never include secrets in `tool.json`: it is public to admitted users. Names and descriptions render as text. Only reviewed tool code is packaged; users cannot choose module paths, commands, images or resource limits.

Test invalid input, bounded RAM, quota exhaustion and cancellation for each new heavy tool. Before exposing it, verify the image and resource/timeout behaviour in a test cluster.

`csv_validation` is an internal tool: excluded from the public catalog and direct tool launches. Its job is bound atomically to one sealed upload. It follows the same resource/deadline/cancellation policy as other heavy tools. See the root README for the precise CSV format and storage paths. Tool code is trusted; do not bypass SDK validation or falsely mark output files as checked.
