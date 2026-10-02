//go:build linux && integration

package snapshotdiff

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"syscall"
	"testing"
	"time"

	diffapi "github.com/containerd/containerd/api/services/diff/v1"
	containerd "github.com/containerd/containerd/v2/client"
	"github.com/containerd/containerd/v2/contrib/diffservice"
	"github.com/containerd/containerd/v2/core/content"
	"github.com/containerd/containerd/v2/core/diff"
	"github.com/containerd/containerd/v2/core/leases"
	"github.com/containerd/containerd/v2/core/mount"
	"github.com/containerd/containerd/v2/core/snapshots"
	"github.com/containerd/containerd/v2/pkg/namespaces"
	"github.com/containerd/errdefs"
	"github.com/opencontainers/go-digest"
	ocispec "github.com/opencontainers/image-spec/specs-go/v1"
	"golang.org/x/sys/unix"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

func runtimeClient(t *testing.T) (context.Context, *containerd.Client) {
	t.Helper()
	address := os.Getenv("ASTRABOX_SNAPSHOT_TEST_CONTAINERD")
	if address == "" || os.Geteuid() != 0 {
		t.Fatal("integration tests require an explicit containerd socket and root in a private mount namespace")
	}
	self, err := os.Readlink("/proc/self/ns/mnt")
	if err != nil {
		t.Fatal(err)
	}
	host, err := os.Readlink("/proc/1/ns/mnt")
	if err != nil {
		t.Fatal(err)
	}
	if self == host {
		t.Fatal("integration tests require a private mount namespace")
	}
	c, err := containerd.New(address)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { c.Close() })
	ctx := namespaces.WithNamespace(context.Background(), "k8s.io")
	ctx, cancel := context.WithTimeout(ctx, 160*time.Second)
	t.Cleanup(cancel)
	ctx, release, err := c.WithLease(ctx, leases.WithRandomID(), leases.WithExpiration(5*time.Minute))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := release(context.WithoutCancel(ctx)); err != nil {
			t.Error(err)
		}
	})
	return ctx, c
}

func prepare(t *testing.T, ctx context.Context, sn snapshots.Snapshotter, parent string, view bool) (string, []mount.Mount) {
	t.Helper()
	key := fmt.Sprintf("astrabox-differ-test-%d", time.Now().UnixNano())
	var mounts []mount.Mount
	var err error
	if view {
		mounts, err = sn.View(ctx, key, parent)
	} else {
		mounts, err = sn.Prepare(ctx, key, parent)
	}
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := sn.Remove(context.WithoutCancel(ctx), key); err != nil && !errdefs.IsNotFound(err) {
			t.Error(err)
		}
	})
	return key, mounts
}

func mounted(t *testing.T, ctx context.Context, mounts []mount.Mount, fn func(string) error) {
	t.Helper()
	if err := mount.WithTempMount(ctx, mounts, fn); err != nil {
		t.Fatal(err)
	}
}

func rpcDiffer(t *testing.T, c *containerd.Client) containerd.DiffService {
	t.Helper()
	path := filepath.Join(t.TempDir(), "diff.sock")
	l, err := net.Listen("unix", path)
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer()
	diffapi.RegisterDiffServer(server, diffservice.FromApplierAndComparer(nil, New(c.ContentStore())))
	go server.Serve(l)
	t.Cleanup(func() { server.Stop(); l.Close() })
	conn, err := grpc.NewClient("unix://"+path, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { conn.Close() })
	return containerd.NewDiffServiceFromClient(diffapi.NewDiffClient(conn))
}

func checkContent(t *testing.T, ctx context.Context, c *containerd.Client, desc ocispec.Descriptor) []byte {
	t.Helper()
	compressed, err := content.ReadBlob(ctx, c.ContentStore(), desc)
	if err != nil {
		t.Fatal(err)
	}
	if int64(len(compressed)) != desc.Size || digest.FromBytes(compressed) != desc.Digest {
		t.Fatal("incorrect compressed descriptor")
	}
	r, err := gzip.NewReader(bytes.NewReader(compressed))
	if err != nil {
		t.Fatal(err)
	}
	defer r.Close()
	raw, err := io.ReadAll(r)
	if err != nil {
		t.Fatal(err)
	}
	info, err := c.ContentStore().Info(ctx, desc.Digest)
	if err != nil {
		t.Fatal(err)
	}
	if info.Labels[uncompressedLabel] != digest.FromBytes(raw).String() {
		t.Fatal("incorrect uncompressed content identity")
	}
	leaseID, ok := leases.FromContext(ctx)
	if !ok {
		t.Fatal("missing test lease")
	}
	resources, err := c.LeasesService().ListResources(ctx, leases.Lease{ID: leaseID})
	if err != nil {
		t.Fatal(err)
	}
	for _, resource := range resources {
		if resource.Type == "content" && resource.ID == desc.Digest.String() {
			return raw
		}
	}
	t.Fatal("gRPC proxy lost caller lease ownership of committed content")
	return nil
}

func TestRPCFastDiffOnActualMultilayerImage(t *testing.T) {
	ctx, c := runtimeClient(t)
	parent := os.Getenv("ASTRABOX_SNAPSHOT_TEST_PARENT")
	if parent == "" {
		t.Fatal("explicit immutable image parent required")
	}
	sn := c.SnapshotService("overlayfs")
	_, lower := prepare(t, ctx, sn, parent, true)
	_, upper := prepare(t, ctx, sn, parent, false)
	layerCount := 0
	for _, option := range upper[0].Options {
		if strings.HasPrefix(option, "lowerdir=") {
			layerCount = len(strings.Split(strings.TrimPrefix(option, "lowerdir="), ":"))
		}
	}
	if layerCount != 98 {
		t.Fatalf("expected diagnosed 98-layer image, got %d", layerCount)
	}
	name := fmt.Sprintf("astrabox-differ-marker-%d", time.Now().UnixNano())
	data := []byte("one real changed file on the diagnosed image\n")
	mounted(t, ctx, upper, func(root string) error { return os.WriteFile(filepath.Join(root, name), data, 0640) })
	started := time.Now()
	desc, err := rpcDiffer(t, c).Compare(ctx, lower, upper)
	if err != nil {
		t.Fatal(err)
	}
	t.Logf("98-layer image diff RPC: %s", time.Since(started))
	if time.Since(started) > 10*time.Second {
		t.Fatal("fast diff walked the unchanged image")
	}
	archive := tar.NewReader(bytes.NewReader(checkContent(t, ctx, c, desc)))
	hdr, err := archive.Next()
	if err != nil {
		t.Fatal(err)
	}
	got, err := io.ReadAll(archive)
	if err != nil {
		t.Fatal(err)
	}
	if hdr.Name != name || hdr.Mode != 0640 || !bytes.Equal(got, data) {
		t.Fatalf("unexpected changed-file archive: %+v %q", hdr, got)
	}
	if _, err := archive.Next(); err != io.EOF {
		t.Fatalf("unexpected additional archive member: %v", err)
	}
}

type entryState struct {
	Mode, UID, GID         uint32
	Data, Link, Capability string
	HardlinkTo             string
}

func treeState(root string) (map[string]entryState, error) {
	result := make(map[string]entryState)
	inodes := make(map[[2]uint64]string)
	err := filepath.WalkDir(root, func(path string, entry os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if path == root {
			return nil
		}
		info, err := entry.Info()
		if err != nil {
			return err
		}
		stat := info.Sys().(*syscall.Stat_t)
		name, err := filepath.Rel(root, path)
		if err != nil {
			return err
		}
		state := entryState{Mode: stat.Mode, UID: stat.Uid, GID: stat.Gid}
		if info.Mode().IsRegular() {
			data, err := os.ReadFile(path)
			if err != nil {
				return err
			}
			hash := sha256.Sum256(data)
			state.Data = hex.EncodeToString(hash[:])
			if stat.Nlink > 1 {
				key := [2]uint64{uint64(stat.Dev), stat.Ino}
				state.HardlinkTo = inodes[key]
				if state.HardlinkTo == "" {
					inodes[key] = name
				}
			}
			cap := make([]byte, 64)
			n, err := unix.Lgetxattr(path, "security.capability", cap)
			if err == nil {
				state.Capability = hex.EncodeToString(cap[:n])
			} else if err != unix.ENODATA {
				return err
			}
		} else if info.Mode()&os.ModeSymlink != 0 {
			state.Link, err = os.Readlink(path)
			if err != nil {
				return err
			}
		}
		result[name] = state
		return nil
	})
	return result, err
}

func TestRPCOverlayRestoreAndNativeApplyFallback(t *testing.T) {
	ctx, c := runtimeClient(t)
	sn := c.SnapshotService("overlayfs")
	seed, mounts := prepare(t, ctx, sn, "", false)
	mounted(t, ctx, mounts, func(root string) error {
		for _, name := range []string{"same", "changed", "deleted", "mode", "cap"} {
			if err := os.WriteFile(filepath.Join(root, name), []byte(name), 0644); err != nil {
				return err
			}
		}
		if err := os.Mkdir(filepath.Join(root, "opaque"), 0750); err != nil {
			return err
		}
		if err := os.WriteFile(filepath.Join(root, "opaque", "old"), []byte("delete"), 0644); err != nil {
			return err
		}
		return os.Symlink("same", filepath.Join(root, "link"))
	})
	base := seed + "-base"
	if err := sn.Commit(ctx, base, seed); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := sn.Remove(context.WithoutCancel(ctx), base); err != nil {
			t.Error(err)
		}
	})
	_, lower := prepare(t, ctx, sn, base, true)
	_, upper := prepare(t, ctx, sn, base, false)
	_, target := prepare(t, ctx, sn, base, false)
	var expected map[string]entryState
	mounted(t, ctx, upper, func(root string) error {
		if err := os.WriteFile(filepath.Join(root, "changed"), []byte("replacement"), 0644); err != nil {
			return err
		}
		if err := os.Remove(filepath.Join(root, "deleted")); err != nil {
			return err
		}
		if err := os.Chmod(filepath.Join(root, "mode"), 0640); err != nil {
			return err
		}
		if err := os.Chown(filepath.Join(root, "mode"), 1234, 2345); err != nil {
			return err
		}
		if err := os.RemoveAll(filepath.Join(root, "opaque")); err != nil {
			return err
		}
		if err := os.Mkdir(filepath.Join(root, "opaque"), 0750); err != nil {
			return err
		}
		if err := os.WriteFile(filepath.Join(root, "opaque", "new"), []byte("replacement"), 0640); err != nil {
			return err
		}
		if err := os.Remove(filepath.Join(root, "link")); err != nil {
			return err
		}
		if err := os.Symlink("changed", filepath.Join(root, "link")); err != nil {
			return err
		}
		if err := os.Link(filepath.Join(root, "changed"), filepath.Join(root, "hardlink")); err != nil {
			return err
		}
		if err := unix.Mkfifo(filepath.Join(root, "fifo"), 0600); err != nil {
			return err
		}
		capability := []byte{1, 0, 0, 2, 0, 4, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0}
		if err := unix.Setxattr(filepath.Join(root, "cap"), "security.capability", capability, 0); err != nil {
			return err
		}
		var err error
		expected, err = treeState(root)
		return err
	})
	service := rpcDiffer(t, c)
	desc, err := service.Compare(ctx, lower, upper)
	if err != nil {
		t.Fatal(err)
	}
	checkContent(t, ctx, c, desc)
	if _, err := service.Apply(ctx, desc, target); !errdefs.IsNotImplemented(err) {
		t.Fatalf("native Apply fallback unavailable: %v", err)
	}
	if _, err := c.DiffService().Apply(ctx, desc, target); err != nil {
		t.Fatal(err)
	}
	mounted(t, ctx, target, func(root string) error {
		actual, err := treeState(root)
		if err != nil {
			return err
		}
		if !reflect.DeepEqual(actual, expected) {
			want, _ := json.Marshal(expected)
			got, _ := json.Marshal(actual)
			return fmt.Errorf("restored tree differs: expected %s actual %s", want, got)
		}
		return nil
	})
}

func TestCancelledDiffAbortsItsUpload(t *testing.T) {
	ctx, c := runtimeClient(t)
	ctx, cancel := context.WithCancel(ctx)
	ref := fmt.Sprintf("astrabox-cancelled-diff-%d", time.Now().UnixNano())
	_, err := New(c.ContentStore()).write(ctx, diff.Config{Reference: ref, MediaType: ocispec.MediaTypeImageLayerGzip}, func(w io.Writer) error {
		if _, err := w.Write([]byte("incomplete archive")); err != nil {
			return err
		}
		cancel()
		return ctx.Err()
	})
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("lost cancellation: %v", err)
	}
	statuses, err := c.ContentStore().ListStatuses(context.WithoutCancel(ctx), "ref=="+ref)
	if err != nil {
		t.Fatal(err)
	}
	if len(statuses) != 0 {
		t.Fatalf("cancelled upload remains: %+v", statuses)
	}
}

func TestMetadataOnlyCopyUpReturnsNativeFallback(t *testing.T) {
	ctx, _ := runtimeClient(t)
	upperdir := t.TempDir()
	path := filepath.Join(upperdir, "metadata-only")
	if err := os.WriteFile(path, nil, 0644); err != nil {
		t.Fatal(err)
	}
	if err := unix.Setxattr(path, "user.overlay.metacopy", []byte("y"), 0); err != nil {
		t.Fatal(err)
	}
	lower := []mount.Mount{{Type: "bind", Source: "/base"}}
	upper := []mount.Mount{{Type: "overlay", Options: []string{"lowerdir=/base", "upperdir=" + upperdir}}}
	if _, err := New(nil).Compare(ctx, lower, upper); !errdefs.IsNotImplemented(err) {
		t.Fatalf("metadata-only file was admitted: %v", err)
	}
}

func TestRepeatedContentPreservesItsIdentityAndCallerLabels(t *testing.T) {
	ctx, c := runtimeClient(t)
	d := New(c.ContentStore())
	labels := map[string]string{"astrabox.test.owner": "snapshot-differ"}
	config := diff.Config{MediaType: ocispec.MediaTypeImageLayerGzip, Labels: labels}
	generate := func(w io.Writer) error { _, err := w.Write([]byte("same deterministic diff bytes")); return err }
	first, err := d.write(ctx, config, generate)
	if err != nil {
		t.Fatal(err)
	}
	second, err := d.write(ctx, config, generate)
	if err != nil {
		t.Fatal(err)
	}
	if first.Digest != second.Digest || first.Size != second.Size {
		t.Fatal("repeated content changed its identity")
	}
	checkContent(t, ctx, c, second)
	info, err := c.ContentStore().Info(ctx, second.Digest)
	if err != nil {
		t.Fatal(err)
	}
	if info.Labels["astrabox.test.owner"] != labels["astrabox.test.owner"] {
		t.Fatal("caller labels were lost")
	}
	if len(labels) != 1 {
		t.Fatal("content writer mutated the caller's labels")
	}
}
