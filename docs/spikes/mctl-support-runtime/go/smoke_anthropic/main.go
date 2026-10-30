// Command smoke_anthropic runs the one fake tool through the official
// Anthropic Go SDK tool runner against a real model. It is never part of the
// gate: without ANTHROPIC_API_KEY it prints "skipped: no key" and exits 0.
// The key is read by the SDK from the environment and never written anywhere.
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"time"

	"github.com/anthropics/anthropic-sdk-go"
	"github.com/anthropics/anthropic-sdk-go/toolrunner"
)

type statusInput struct {
	Service string `json:"service" jsonschema:"required,description=Service name"`
}

func main() {
	// ANTHROPIC_AUTH_TOKEN (Bearer) serves Anthropic-compatible endpoints such
	// as Hetzner inference together with ANTHROPIC_BASE_URL; the SDK reads both.
	if os.Getenv("ANTHROPIC_API_KEY") == "" && os.Getenv("ANTHROPIC_AUTH_TOKEN") == "" {
		fmt.Println("skipped: no key")
		return
	}
	model := os.Getenv("ANTHROPIC_MODEL")
	if model == "" {
		model = "claude-sonnet-5-5"
	}

	tool, err := toolrunner.NewBetaToolFromJSONSchema("get_service_status",
		"Returns the current status of a tenant service",
		func(_ context.Context, in statusInput) (anthropic.BetaToolResultBlockParamContentUnion, error) {
			out, _ := json.Marshal(map[string]any{"service": in.Service, "status": "degraded", "replicas": 1})
			return anthropic.BetaToolResultBlockParamContentUnion{OfText: &anthropic.BetaTextBlockParam{Text: string(out)}}, nil
		})
	if err != nil {
		fmt.Fprintln(os.Stderr, "tool:", err)
		os.Exit(1)
	}

	client := anthropic.NewClient() // ANTHROPIC_API_KEY from env
	runner := client.Beta.Messages.NewToolRunner([]anthropic.BetaTool{tool}, anthropic.BetaToolRunnerParams{
		BetaMessageNewParams: anthropic.BetaMessageNewParams{
			Model:     anthropic.Model(model),
			MaxTokens: 512,
			System:    []anthropic.BetaTextBlockParam{{Text: "You are a read-only support agent. Use get_service_status to answer, then reply in one sentence."}},
			Messages:  []anthropic.BetaMessageParam{anthropic.NewBetaUserMessage(anthropic.NewBetaTextBlock("status of svc-x"))},
		},
	})

	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	final, err := runner.RunToCompletion(ctx)
	if err != nil {
		fmt.Fprintln(os.Stderr, "anthropic:", err)
		os.Exit(1)
	}
	fmt.Printf("model=%s iterations=%d\n", model, runner.IterationCount())
	for _, block := range final.Content {
		if t, ok := block.AsAny().(anthropic.BetaTextBlock); ok {
			fmt.Println(t.Text)
		}
	}
}
