# AstraBox — the harness composition every dsh-superagent box boots. Sourced by the
# image's programs (astrabox-dsh-superagent-serve, astrabox-dsh-superagent-boot-check).
#
# The bundle's own layer, then AstraBox's, then each layer an image built on this one
# adds under /opt/astrabox/superagent/patches.d, in byte order of their names. A derived
# image inserts its own Source plugins there; the plugins live under
# /opt/astrabox/dsh-plugins, where `dsh-superagent/sources` resolves to the installed
# bundle (docs/assistants.md, "Your own Sources").

SUPERAGENT_LAYERS_DIR=/opt/astrabox/superagent/patches.d

superagent_patch_args() {
  local LC_ALL=C layer
  SUPERAGENT_PATCH_ARGS=(
    --patch /opt/dsh/superagent-bundle.patch.yml
    --patch /opt/dsh/superagent.patch.yml
  )
  for layer in "$SUPERAGENT_LAYERS_DIR"/*.yml; do
    [ -e "$layer" ] || continue
    SUPERAGENT_PATCH_ARGS+=(--patch "$layer")
  done
}
