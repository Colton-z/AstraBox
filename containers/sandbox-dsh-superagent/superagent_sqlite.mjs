// Copy and check dsh-superagent's database with the SQLite the bundle itself runs on.
//
// The bundle keeps its state through Node's built-in SQLite, whose schema uses
// syntax (the `->>` JSON operator) the base image's Python SQLite (3.37) cannot
// parse, so a copy made or checked there reports a malformed schema.
//
//   node superagent_sqlite.mjs snapshot <database> <copy>   online backup, then check the copy
//   node superagent_sqlite.mjs check <database>              integrity check only
import { DatabaseSync, backup } from 'node:sqlite'

function check(path) {
  const db = new DatabaseSync(path)
  try {
    const verdict = db.prepare('PRAGMA integrity_check').all().map((row) => Object.values(row)[0]).join('; ')
    if (verdict !== 'ok') throw new Error(`dsh-superagent database failed its integrity check: ${verdict}`)
  } finally {
    db.close()
  }
}

const [action, source, destination] = process.argv.slice(2)
if (action === 'snapshot' && source && destination) {
  const db = new DatabaseSync(source)
  try {
    await backup(db, destination)
  } finally {
    db.close()
  }
  check(destination)
} else if (action === 'check' && source) {
  check(source)
} else {
  console.error('usage: superagent_sqlite.mjs snapshot <database> <copy> | check <database>')
  process.exit(64)
}
