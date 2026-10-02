//go:build linux

package main

import (
	"context"
	"flag"
	"fmt"
	"log/slog"
	"net"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	snapshotdiff "github.com/colton-z/astrabox/tools/snapshot-differ"
	diffapi "github.com/containerd/containerd/api/services/diff/v1"
	containerd "github.com/containerd/containerd/v2/client"
	"github.com/containerd/containerd/v2/contrib/diffservice"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/health"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
)

func run() error {
	address := flag.String("address", "/run/astrabox-snapshot-differ/diff.sock", "private Unix socket to create")
	daemon := flag.String("containerd-address", "/run/containerd/containerd.sock", "containerd content service socket")
	check := flag.Bool("check", false, "check the existing differ socket without changing it")
	flag.Parse()
	if flag.NArg() != 0 {
		return fmt.Errorf("unexpected positional arguments")
	}
	parent, err := os.Stat(filepath.Dir(*address))
	if err != nil {
		return fmt.Errorf("read private socket directory: %w", err)
	}
	owner, ok := parent.Sys().(*syscall.Stat_t)
	if !ok || !parent.IsDir() || owner.Uid != uint32(os.Geteuid()) || parent.Mode().Perm()&0077 != 0 {
		return fmt.Errorf("socket directory must be private and owned by the service user")
	}
	if *check {
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		conn, err := grpc.NewClient("unix://"+*address, grpc.WithTransportCredentials(insecure.NewCredentials()))
		if err != nil {
			return err
		}
		defer conn.Close()
		result, err := healthpb.NewHealthClient(conn).Check(ctx, &healthpb.HealthCheckRequest{})
		if err != nil {
			return err
		}
		if result.Status != healthpb.HealthCheckResponse_SERVING {
			return fmt.Errorf("differ is not serving")
		}
		return nil
	}
	// Refuse to replace another listener. The service manager owns stale-socket
	// removal and the parent directory's permissions.
	if _, err := os.Lstat(*address); !os.IsNotExist(err) {
		return fmt.Errorf("socket path must not exist: %s", *address)
	}
	client, err := containerd.New(*daemon)
	if err != nil {
		return err
	}
	defer client.Close()
	listener, err := net.Listen("unix", *address)
	if err != nil {
		return err
	}
	defer listener.Close()
	if err := os.Chmod(*address, 0o600); err != nil {
		return err
	}
	server := grpc.NewServer()
	// Apply stays with containerd's native differ, including its configured
	// unpack processors. Nil maps to gRPC Unimplemented and native fallback.
	diffapi.RegisterDiffServer(server, diffservice.FromApplierAndComparer(nil, snapshotdiff.New(client.ContentStore())))
	healthService := health.NewServer()
	healthService.SetServingStatus("", healthpb.HealthCheckResponse_SERVING)
	healthpb.RegisterHealthServer(server, healthService)
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	go func() {
		<-ctx.Done()
		finished := make(chan struct{})
		go func() { server.GracefulStop(); close(finished) }()
		select {
		case <-finished:
		case <-time.After(10 * time.Second):
			server.Stop()
		}
	}()
	slog.Info("snapshot differ listening", "address", *address)
	return server.Serve(listener)
}

func main() {
	if err := run(); err != nil {
		slog.Error("snapshot differ stopped", "error", err)
		os.Exit(1)
	}
}
