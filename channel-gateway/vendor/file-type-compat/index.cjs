'use strict'

async function modern() {
  return import('file-type-modern')
}

module.exports = {
  async fromBuffer(input) {
    return (await modern()).fileTypeFromBuffer(input)
  },
  async fromStream(input) {
    return (await modern()).fileTypeFromStream(input)
  },
  async fromBlob(input) {
    return (await modern()).fileTypeFromBlob(input)
  },
}
