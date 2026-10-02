//go:build linux

package snapshotdiff

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/containerd/containerd/v2/core/diff"
	"github.com/containerd/containerd/v2/core/mount"
	"github.com/containerd/errdefs"
	ocispec "github.com/opencontainers/image-spec/specs-go/v1"
)

func TestUnsupportedRequestsLeaveNativeFallbackAvailable(t *testing.T) {
	base := []mount.Mount{{Type: "bind", Source: "/base"}}
	upper := []mount.Mount{{Type: "overlay", Options: []string{"lowerdir=/base", "upperdir=/changed", "workdir=/work"}}}
	now := time.Now()
	for _, test := range []struct {
		name  string
		upper []mount.Mount
		opts  []diff.Opt
	}{
		{"uncompressed", upper, []diff.Opt{diff.WithMediaType(ocispec.MediaTypeImageLayer)}},
		{"reproducible timestamp", upper, []diff.Opt{diff.WithSourceDateEpoch(&now)}},
		{"non-overlay", base, nil},
		{"unknown option", []mount.Mount{{Type: "overlay", Options: []string{"lowerdir=/base", "upperdir=/changed", "metacopy=on"}}}, nil},
		{"different parents", []mount.Mount{{Type: "overlay", Options: []string{"lowerdir=/foreign", "upperdir=/changed"}}}, nil},
	} {
		t.Run(test.name, func(t *testing.T) {
			// A nil store also proves the refused request opened no content writer.
			_, err := New(nil).Compare(context.Background(), base, test.upper, test.opts...)
			if !errdefs.IsNotImplemented(err) {
				t.Fatalf("expected native fallback, got %v", err)
			}
		})
	}
}

func TestInvalidOptionIsNotSilentlyRetried(t *testing.T) {
	want := errors.New("invalid requested encoding")
	_, err := New(nil).Compare(context.Background(), nil, nil, func(*diff.Config) error { return want })
	if !errors.Is(err, want) || errdefs.IsNotImplemented(err) {
		t.Fatalf("lost original refusal: %v", err)
	}
}
