# Attach and download files

> Upload files to give your Agent context, and download files it produces.

The Files API lets you attach files — code repositories, configuration, reference docs — to a Session for the Agent to read during task execution. The console exposes the same workspace in the **Files** tab. Files are available at their workspace paths as soon as they are uploaded; there is no separate File resource or mount step.

## Workflow

1. <b>Open a Session</b> Start a Session with the Agent that will use the files.
2. <b>Upload file</b> Open **Files**, select a directory, then upload or drag in one or more files.
3. <b>Agent uses it</b> The Agent reads the file's contents during the Session and completes the task.

## Upload a file

```text
POST /api/v1/sessions/{session_id}/files/upload
Content-Type: multipart/form-data
```

### Parameters

| Field   | Type     | Required | Description                                      |
| ------- | -------- | -------- | ------------------------------------------------ |
| `files` | binary[] | Yes      | One or more files                                |
| `path`  | string   | No       | Destination directory; the Session root by default |

### File Operations

| Action     | Meaning                                      |
| ---------- | -------------------------------------------- |
| Upload     | Add one or more files to the selected folder |
| New folder | Create a directory in the workspace          |
| Rename     | Rename or move a file or directory           |
| Delete     | Delete a file or directory                   |
| Download   | Download a regular file                      |

> A regular file up to 64 MiB can be downloaded. Directories are managed in
> place rather than downloaded as archives.

## Upload examples

```bash
curl -X POST \
  "$SERVICE_URL/api/v1/sessions/$SESSION_ID/files/upload" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -F "files=@./src/main.py"
```

Response:

```json
{
  "code": "OK",
  "message": "success",
  "data": {
    "root_path": "/workspace",
    "current_path": "/workspace",
    "parent_path": null,
    "entries": [
      {
        "path": "/workspace/main.py",
        "name": "main.py",
        "kind": "file"
      }
    ],
    "uploaded_count": 1
  }
}
```

Uploading multiple files:

```bash
curl -X POST \
  "$SERVICE_URL/api/v1/sessions/$SESSION_ID/files/upload" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -F "files=@./config.yaml" \
  -F "files=@./requirements.txt"
```

## Use files in a Session

The upload endpoint is scoped to a Session, so each uploaded file is already in
that Session's workspace. The standard AstraBox Agent images expose the
workspace as `/workspace`, and the **Files** tab always shows the effective
root and current directory.

### Prompt example

After uploading `app.py`, send the task in the same Session:

```text
Review /workspace/app.py and fix the bugs. Save the corrected file in place
and write a summary to /workspace/review.md.
```

The Agent can open `app.py` immediately and any files it creates appear in the
same workspace.

## Managing files

### Download a file

Open the file's action menu in **Files** and select **Download**, or call:

```bash
curl --get \
  "$SERVICE_URL/api/v1/sessions/$SESSION_ID/files/download" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  --data-urlencode "path=/workspace/review.md" \
  -o review.md
```

Any regular file in the workspace can be downloaded when it is no larger than
64 MiB.

### Inspect file metadata

The **Files** tab shows the current path, directory tree, file names, file
types, and sizes. These values come from the live Session workspace, so Agent
changes appear at the same paths.

### List files

Expand folders in **Files** to browse the workspace. The open directories
refresh after a turn completes; select **Refresh** to reload them at any time.

The corresponding API operation lists one directory at a time:

```text
POST /api/v1/sessions/{session_id}/files/list
Content-Type: application/json
```

```bash
curl -X POST \
  "$SERVICE_URL/api/v1/sessions/$SESSION_ID/files/list" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"path": "/workspace"}'
```

## End-to-end example

1. Open an Agent and start a Session.
2. Open **Files**, select the workspace root, and upload `app.py`.
3. Ask the Agent to review `app.py`, fix the bugs, and save a report as `review.md`.
4. When the task completes, open `review.md` in **Files** and select **Download**.

## FAQ

<b>Q: How long are uploaded files retained?</b>

A: Files belong to the Session workspace. With optional persistent workspace
storage configured, they can survive sandbox release or replacement. Without
it, files stored only in the sandbox are lost when that sandbox is removed.
The platform database preserves native conversation state separately; restoring
a conversation does not restore workspace files. Download important results or
commit and push repository changes when they need an independent copy.

<b>Q: Can I attach files when creating a Session?</b>

A: Start the Session, wait for its runtime to be ready, then upload the files.
They are written directly into the workspace, with no later mount operation.

<b>Q: Which files can I download?</b>

A: Any regular file in the Session workspace can be downloaded when it is no
larger than 64 MiB, whether it was uploaded by a user or produced by the Agent.

<b>Q: Which file formats are supported?</b>

A: Any binary file is accepted. Text-based files (code, configuration, documents) yield the best results.

## Next steps

- [Sessions](sessions.md) — Start a Session before uploading workspace files.
- [Access GitHub](working-with-repos.md) — Prepare a repository checkout for a
  Session.
- [Assistants](assistants.md) — Use files in a long-lived personal workspace.
