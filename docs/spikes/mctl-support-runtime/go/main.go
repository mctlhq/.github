// Command spike-mctl-support-go is the Go candidate of the mctl-support
// runtime spike (.github#249): an A2A 1.0 HTTP+JSON server on a2a-go v2.7.0
// with bearer auth, a Postgres task store and a deterministic fake tool loop.
package main

import (
	"context"
	"log"
	"net/http"
	"os"
	"time"

	"github.com/a2aproject/a2a-go/v2/a2a"
	"github.com/a2aproject/a2a-go/v2/a2asrv"
)

func env(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func main() {
	port := env("PORT", "8011")
	replica := env("REPLICA", "a")
	dsn := env("DATABASE_URL", "postgres://spike:spike@127.0.0.1:55432/spike_go?sslmode=disable")
	log.SetFlags(log.Ltime | log.Lmicroseconds)
	log.SetPrefix("[" + replica + "] ")

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	store, err := newPGStore(ctx, dsn, a2asrv.NewTaskStoreAuthenticator())
	cancel()
	if err != nil {
		log.Fatalf("task store: %v", err)
	}
	defer store.Close()

	executor := &supportExecutor{store: store, model: fakeModel{}, tools: fakeTools()}
	handler := a2asrv.NewHandler(executor,
		a2asrv.WithTaskStore(store),
		a2asrv.WithCallInterceptors(
			&traceInterceptor{replica: replica}, // first: its After runs last and on every early exit
			&bearerAuth{},
			&contextOwnerGuard{store: store},
		),
	)

	base := "http://127.0.0.1:" + port + "/v1"
	card := &a2a.AgentCard{
		Name:        "mctl-support (go spike)",
		Description: "Read-only tenant-bound support agent prototype on a2a-go v2.7.0",
		Version:     "0.0.1",
		SupportedInterfaces: []*a2a.AgentInterface{
			a2a.NewAgentInterface(base, a2a.TransportProtocolHTTPJSON),
		},
		DefaultInputModes:  []string{"text/plain"},
		DefaultOutputModes: []string{"text/plain"},
		Capabilities:       a2a.AgentCapabilities{Streaming: true},
		SecuritySchemes: a2a.NamedSecuritySchemes{
			"bearer": a2a.HTTPAuthSecurityScheme{Scheme: "bearer", Description: "spike-<principal>"},
		},
		SecurityRequirements: a2a.SecurityRequirementsOptions{{"bearer": {}}},
		Skills: []a2a.AgentSkill{{
			ID:          "service_status",
			Name:        "Service status",
			Description: "Reports the status of a tenant service through get_service_status",
			Tags:        []string{"support", "read-only"},
			Examples:    []string{"status of api", "and again?"},
		}},
	}

	mux := http.NewServeMux()
	mux.Handle(a2asrv.WellKnownAgentCardPath, a2asrv.NewStaticAgentCardHandler(card))
	mux.Handle("/v1/", http.StripPrefix("/v1", a2asrv.NewRESTHandler(handler)))

	log.Printf("listening on :%s (REST base %s, replica %s)", port, base, replica)
	srv := &http.Server{Addr: ":" + port, Handler: mux, ReadHeaderTimeout: 5 * time.Second}
	if err := srv.ListenAndServe(); err != nil {
		log.Fatal(err)
	}
}
