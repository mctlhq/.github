package model

import "time"

// ResolveLocal turns a local wall time into an instant under an explicit,
// named DST product policy. Defaults mirror what the real dev server
// OBSERVED (docs report D4): a gap occurrence is skipped; an ambiguous
// occurrence fires once, at the later instant. Both are overridable.
func ResolveLocal(y int, mo time.Month, d, h, mi int, loc *time.Location, gap, pick string) (time.Time, bool) {
	t := time.Date(y, mo, d, h, mi, 0, 0, loc)
	wall := func(x time.Time) bool {
		x = x.In(loc)
		return x.Year() == y && x.Month() == mo && x.Day() == d && x.Hour() == h && x.Minute() == mi
	}
	if !wall(t) { // gap: Go normalizes forward, which is the shift_forward policy
		if gap == "shift_forward" {
			return t, true
		}
		return time.Time{}, false
	}
	best := t
	for _, off := range []time.Duration{-time.Hour, time.Hour} {
		if alt := t.Add(off); wall(alt) {
			if pick == "first" && alt.Before(best) || pick != "first" && alt.After(best) {
				best = alt
			}
		}
	}
	return best, true
}

func (tr Trigger) loc() *time.Location {
	if tr.TZ == "" {
		return time.UTC
	}
	l, err := time.LoadLocation(tr.TZ)
	if err != nil {
		return time.UTC
	}
	return l
}

// AtInstant is the single well-defined instant of an `at` trigger.
func (tr Trigger) AtInstant() (time.Time, bool) {
	if !tr.At.IsZero() {
		return tr.At, true
	}
	loc := tr.loc()
	lt, err := time.Parse("2006-01-02T15:04", tr.LocalWall) // wall fields only; zone applied by ResolveLocal
	if err != nil {
		return time.Time{}, false
	}
	return ResolveLocal(lt.Year(), lt.Month(), lt.Day(), lt.Hour(), lt.Minute(), loc, tr.GapPolicy, tr.OverlapPick)
}

// Occurrences lists nominal fire instants in (after, upto].
func (tr Trigger) Occurrences(after, upto time.Time) []time.Time {
	var out []time.Time
	switch tr.Kind {
	case TrigAt:
		if t, ok := tr.AtInstant(); ok && t.After(after) && !t.After(upto) {
			out = append(out, t)
		}
	case TrigSchedule:
		loc := tr.loc()
		day := after.In(loc).AddDate(0, 0, -1)
		for i := 0; i < 400; i++ {
			dd := day.AddDate(0, 0, i)
			if dd.Weekday() != tr.Weekday {
				continue
			}
			t, ok := ResolveLocal(dd.Year(), dd.Month(), dd.Day(), tr.Hour, tr.Minute, loc, tr.GapPolicy, tr.OverlapPick)
			if !ok || !t.After(after) {
				continue
			}
			if t.After(upto) {
				break
			}
			out = append(out, t)
		}
	}
	return out
}
