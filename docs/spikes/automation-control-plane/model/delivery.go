package model

// DeliveryAdapter is the Telegram-first delivery port (mock).
type DeliveryAdapter interface{ Send(chat, text string) error }

// DeliveryResult is the recorded, surface-readable outcome.
type DeliveryResult struct {
	Status   string // delivered | failed
	Attempts int
}

// Deliver retries a bounded number of times, then records failure (never silent).
func Deliver(a DeliveryAdapter, chat, text string, maxAttempts int) DeliveryResult {
	r := DeliveryResult{Status: "failed"}
	for r.Attempts < maxAttempts {
		r.Attempts++
		if a.Send(chat, text) == nil {
			r.Status = "delivered"
			return r
		}
	}
	return r
}
