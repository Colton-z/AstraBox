//go:build linux

// Package snapshotdiff generates OCI layers from overlayfs writable layers.
package snapshotdiff

import (
	"compress/gzip"
	"context"
	"crypto/rand"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/containerd/containerd/v2/core/content"
	"github.com/containerd/containerd/v2/core/diff"
	"github.com/containerd/containerd/v2/core/leases"
	"github.com/containerd/containerd/v2/core/mount"
	"github.com/containerd/containerd/v2/pkg/epoch"
	"github.com/containerd/containerd/v2/pkg/namespaces"
	"github.com/containerd/errdefs"
	"github.com/moby/buildkit/util/overlay"
	"github.com/opencontainers/go-digest"
	ocispec "github.com/opencontainers/image-spec/specs-go/v1"
	"golang.org/x/sys/unix"
)

const uncompressedLabel = "containerd.io/uncompressed"
const cleanupTimeout = 10 * time.Second

// Differ supports a single overlayfs writable layer and gzip OCI output.
// Unsupported mount layouts return ErrNotImplemented before opening content,
// allowing containerd to select its configured walking differ.
type Differ struct {
	store content.Store
}

func New(store content.Store) *Differ { return &Differ{store: store} }

func (d *Differ) Compare(ctx context.Context, lower, upper []mount.Mount, opts ...diff.Opt) (ocispec.Descriptor, error) {
	var config diff.Config
	for _, opt := range opts {
		if err := opt(&config); err != nil {
			return ocispec.Descriptor{}, err
		}
	}
	if config.MediaType == "" {
		config.MediaType = ocispec.MediaTypeImageLayerGzip
	}
	if config.MediaType != ocispec.MediaTypeImageLayerGzip || config.Compressor != nil || config.SourceDateEpoch != nil || epoch.FromContext(ctx) != nil {
		return ocispec.Descriptor{}, fmt.Errorf("diff encoding requires the native differ: %w", errdefs.ErrNotImplemented)
	}
	if len(upper) != 1 || upper[0].Type != "overlay" {
		return ocispec.Descriptor{}, fmt.Errorf("not an overlayfs writable layer: %w", errdefs.ErrNotImplemented)
	}
	upperdir, err := overlay.GetUpperdir(lower, upper)
	if err != nil {
		return ocispec.Descriptor{}, fmt.Errorf("unsupported overlay layout: %v: %w", err, errdefs.ErrNotImplemented)
	}
	if err := checkCopyUp(ctx, upperdir); err != nil {
		return ocispec.Descriptor{}, err
	}
	// Incoming gRPC metadata must also be attached to the outgoing content RPCs.
	namespace, err := namespaces.NamespaceRequired(ctx)
	if err != nil {
		return ocispec.Descriptor{}, err
	}
	ctx = namespaces.WithNamespace(ctx, namespace)
	if lease, ok := leases.FromContext(ctx); ok {
		ctx = leases.WithLease(ctx, lease)
	}
	return d.write(ctx, config, func(w io.Writer) error {
		return overlay.WriteUpperdir(ctx, w, upperdir, lower)
	})
}

// A metadata-only copy-up needs lower-file data that an isolated upper view
// cannot supply. Reject it instead of silently archiving an empty file.
func checkCopyUp(ctx context.Context, upperdir string) error {
	setting, err := os.ReadFile("/sys/module/overlay/parameters/metacopy")
	if err != nil || strings.TrimSpace(string(setting)) != "N" {
		return fmt.Errorf("overlay metacopy must be disabled: %w", errdefs.ErrNotImplemented)
	}
	return filepath.WalkDir(upperdir, func(path string, entry fs.DirEntry, walkErr error) error {
		if err := ctx.Err(); err != nil {
			return err
		}
		if walkErr != nil {
			return walkErr
		}
		for _, name := range []string{"trusted.overlay.metacopy", "user.overlay.metacopy", "trusted.overlay.redirect", "user.overlay.redirect"} {
			_, err := unix.Lgetxattr(path, name, nil)
			switch err {
			case unix.ENODATA:
				continue
			case nil:
				return fmt.Errorf("overlay metadata requires the native differ: %w", errdefs.ErrNotImplemented)
			case unix.EOPNOTSUPP:
				return fmt.Errorf("overlay metadata cannot be checked: %w", errdefs.ErrNotImplemented)
			default:
				return fmt.Errorf("inspect overlay metadata: %w", err)
			}
		}
		return nil
	})
}

func (d *Differ) write(ctx context.Context, config diff.Config, generate func(io.Writer) error) (desc ocispec.Descriptor, retErr error) {
	ref := config.Reference
	if ref == "" {
		var id [16]byte
		if _, err := rand.Read(id[:]); err != nil {
			return ocispec.Descriptor{}, err
		}
		ref = fmt.Sprintf("astrabox-overlay-%x", id)
	}
	w, err := d.store.Writer(ctx, content.WithRef(ref))
	if err != nil {
		return ocispec.Descriptor{}, fmt.Errorf("open diff content: %w", err)
	}
	committed := false
	defer func() {
		w.Close()
		if !committed {
			// A cancelled request must still release its incomplete upload.
			cleanup, cancel := context.WithTimeout(context.WithoutCancel(ctx), cleanupTimeout)
			defer cancel()
			if err := d.store.Abort(cleanup, ref); err != nil && !errdefs.IsNotFound(err) {
				retErr = errors.Join(retErr, fmt.Errorf("clean incomplete diff: %w", err))
			}
		}
	}()
	if err := w.Truncate(0); err != nil {
		return ocispec.Descriptor{}, err
	}
	uncompressed := digest.SHA256.Digester()
	compressed := gzip.NewWriter(w)
	writeErr := generate(io.MultiWriter(compressed, uncompressed.Hash()))
	closeErr := compressed.Close()
	if writeErr != nil {
		return ocispec.Descriptor{}, fmt.Errorf("generate overlay diff: %w", writeErr)
	}
	if closeErr != nil {
		return ocispec.Descriptor{}, fmt.Errorf("finish compressed diff: %w", closeErr)
	}
	labels := make(map[string]string, len(config.Labels)+1)
	for key, value := range config.Labels {
		labels[key] = value
	}
	labels[uncompressedLabel] = uncompressed.Digest().String()
	dgst := w.Digest()
	if err := w.Commit(ctx, 0, dgst, content.WithLabels(labels)); err != nil && !errdefs.IsAlreadyExists(err) {
		return ocispec.Descriptor{}, fmt.Errorf("commit overlay diff: %w", err)
	}
	committed = true
	info, err := d.store.Info(ctx, dgst)
	if err != nil {
		return ocispec.Descriptor{}, err
	}
	if existing := info.Labels[uncompressedLabel]; existing != "" && existing != labels[uncompressedLabel] {
		return ocispec.Descriptor{}, fmt.Errorf("existing diff has a conflicting uncompressed digest")
	}
	if info.Labels[uncompressedLabel] == "" {
		if info.Labels == nil {
			info.Labels = make(map[string]string)
		}
		info.Labels[uncompressedLabel] = labels[uncompressedLabel]
		if _, err := d.store.Update(ctx, info, "labels."+uncompressedLabel); err != nil {
			return ocispec.Descriptor{}, err
		}
	}
	return ocispec.Descriptor{MediaType: config.MediaType, Digest: dgst, Size: info.Size}, nil
}
