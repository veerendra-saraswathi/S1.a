// Package clock maps an autopilot's boot clock onto UTC, so telemetry is stamped
// with when it was sampled on the vehicle, not when it reached the gateway.
//
// MAVLink's timed messages carry time_boot_ms, milliseconds since the autopilot
// booted. That is exact for ordering and spacing one vehicle's samples, but it is
// not wall time. A clock turns it into UTC by learning the vehicle's boot epoch,
// the UTC instant at which time_boot_ms was zero, from the best evidence it has:
//
//	gnss       SYSTEM_TIME (#2) pairs the autopilot's UTC, normally from its GNSS
//	           receiver, with time_boot_ms. epoch = time_unix_usec - time_boot_ms.
//	           Refreshed on every SYSTEM_TIME, so crystal drift between them is the
//	           only error. Trusted for StaleAfter after the last one.
//	estimated  No usable SYSTEM_TIME. epoch = min(arrival - time_boot_ms) over a
//	           sliding window. The minimum strips network and queueing jitter,
//	           leaving the shortest link delay, so samples keep their exact relative
//	           spacing and land on the gateway's clock a few milliseconds early.
//	           The window lets it follow the autopilot crystal's drift.
//	gateway    No timed message seen: the caller falls back to arrival time.
//
// The gateway only listens, so it cannot run MAVLink TIMESYNC, which needs the
// gateway to send. That keeps the observe-only rule (no transmit path) intact.
//
// A time_boot_ms that goes backwards by more than RebootJump means the autopilot
// rebooted; everything learned about the old boot is discarded.
package clock

import "time"

// Source says how a timestamp was obtained. It travels with the telemetry.
type Source string

const (
	GNSS      Source = "gnss"
	Estimated Source = "estimated"
	Gateway   Source = "gateway"
)

const (
	// StaleAfter is how long a SYSTEM_TIME anchor is trusted without a fresh one.
	StaleAfter = 5 * time.Second
	// RebootJump is how far time_boot_ms may step back before it counts as a reboot,
	// rather than a reordered datagram.
	RebootJump = time.Second
	// Window is how far back the estimated epoch looks for its minimum.
	Window = 10 * time.Second

	buckets = 10 // Window is kept as this many slices
)

// earliestUnix rejects an autopilot UTC before 2020: an RTC that was never set.
var earliestUnix = time.Date(2020, 1, 1, 0, 0, 0, 0, time.UTC)

type bucket struct {
	start time.Time // arrival time the slice opened
	epoch time.Time // smallest arrival - boot seen in the slice
	ok    bool
}

// Clock is one vehicle's boot clock. It is not safe for concurrent use; the
// aggregator holds its lock while calling it.
type Clock struct {
	haveBoot bool
	lastBoot uint32

	gnssEpoch  time.Time // from SYSTEM_TIME
	gnssAt     time.Time // arrival of the SYSTEM_TIME that set it
	gnssOffset time.Duration
	haveGNSS   bool

	window [buckets]bucket
	head   int
}

// Observe records that a timed message sampled at bootMs arrived at arrival.
// It reports whether this message started a new boot.
func (c *Clock) Observe(bootMs uint32, arrival time.Time) (rebooted bool) {
	if c.haveBoot && int64(bootMs)+RebootJump.Milliseconds() < int64(c.lastBoot) {
		*c = Clock{}
		rebooted = true
	}
	if !c.haveBoot || bootMs > c.lastBoot {
		c.lastBoot = bootMs
	}
	c.haveBoot = true

	epoch := arrival.Add(-ms(bootMs))
	b := &c.window[c.head]
	if !b.ok || arrival.Sub(b.start) >= Window/buckets {
		c.head = (c.head + 1) % buckets
		c.window[c.head] = bucket{start: arrival, epoch: epoch, ok: true}
	} else if epoch.Before(b.epoch) {
		b.epoch = epoch
	}
	return rebooted
}

// SystemTime records a SYSTEM_TIME frame. A zero or implausible UTC is ignored
// for the GNSS anchor, but its boot time still feeds the estimate.
func (c *Clock) SystemTime(unixUsec uint64, bootMs uint32, arrival time.Time) {
	c.Observe(bootMs, arrival)
	utc := time.UnixMicro(int64(unixUsec)).UTC()
	if unixUsec == 0 || utc.Before(earliestUnix) {
		return
	}
	c.gnssEpoch = utc.Add(-ms(bootMs))
	c.gnssAt = arrival
	// Positive: the vehicle's clock is ahead of the gateway's. It includes the
	// one-way link delay, so a vehicle in perfect sync reads a few ms behind.
	c.gnssOffset = utc.Sub(arrival)
	c.haveGNSS = true
}

// At converts a boot time to UTC on the best clock available as of now.
// ok is false when no timed message has been seen, and the caller should use
// arrival time with source Gateway.
func (c *Clock) At(bootMs uint32, now time.Time) (utc time.Time, source Source, ok bool) {
	if c.gnssValid(now) {
		return c.gnssEpoch.Add(ms(bootMs)), GNSS, true
	}
	if epoch, ok := c.estimatedEpoch(now); ok {
		return epoch.Add(ms(bootMs)), Estimated, true
	}
	return time.Time{}, Gateway, false
}

// Offset is the vehicle's UTC minus the gateway's arrival time, from the latest
// SYSTEM_TIME, while that anchor is still trusted.
func (c *Clock) Offset(now time.Time) (time.Duration, bool) {
	if !c.gnssValid(now) {
		return 0, false
	}
	return c.gnssOffset, true
}

func (c *Clock) gnssValid(now time.Time) bool {
	return c.haveGNSS && now.Sub(c.gnssAt) <= StaleAfter
}

func (c *Clock) estimatedEpoch(now time.Time) (time.Time, bool) {
	var best time.Time
	found := false
	for _, b := range c.window {
		if !b.ok || now.Sub(b.start) > Window {
			continue
		}
		if !found || b.epoch.Before(best) {
			best, found = b.epoch, true
		}
	}
	return best, found
}

func ms(v uint32) time.Duration { return time.Duration(v) * time.Millisecond }
