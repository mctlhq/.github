package model

import "sync"

// Ledger models the usage ledger: reservations idempotent on the execution key.
type Ledger struct {
	mu       sync.Mutex
	res      map[string]int64 // key -> reserved minor
	settled  map[string]bool
	ownerDay map[string]int64
	Reserves int // number of distinct reservations actually made
}

func NewLedger() *Ledger {
	return &Ledger{res: map[string]int64{}, settled: map[string]bool{}, ownerDay: map[string]int64{}}
}

// Reserve is idempotent on key. It refuses before any agent call.
func (l *Ledger) Reserve(key, owner string, amount int64, b Budget) error {
	l.mu.Lock()
	defer l.mu.Unlock()
	if _, ok := l.res[key]; ok {
		return nil
	}
	if amount > b.PerRunMinor || l.ownerDay[owner]+amount > b.PerOwnerDayMinor {
		return ErrBudgetExceeded
	}
	l.res[key] = amount
	l.ownerDay[owner] += amount
	l.Reserves++
	return nil
}

func (l *Ledger) Settle(key string) { l.mu.Lock(); l.settled[key] = true; l.mu.Unlock() }
func (l *Ledger) Release(key, owner string) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if a, ok := l.res[key]; ok && !l.settled[key] {
		l.ownerDay[owner] -= a
		delete(l.res, key)
	}
}
func (l *Ledger) Settled(key string) bool { l.mu.Lock(); defer l.mu.Unlock(); return l.settled[key] }
