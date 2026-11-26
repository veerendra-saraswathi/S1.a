package s2

import (
	"encoding/json"
	"os"
	"reflect"
	"regexp"
	"sort"
	"strings"
	"testing"
	"time"
)

// The pinned copy of S2's frozen contract (make check-contract compares it with S2's).
const v1 = "../../../contracts/telemetry/v1/"

type schema struct {
	Pattern    string `json:"pattern"`
	Required   []string
	Properties map[string]struct {
		Pattern       string `json:"pattern"`
		PropertyNames struct {
			Pattern string `json:"pattern"`
		} `json:"propertyNames"`
	}
	Defs map[string]struct {
		Required   []string
		Properties map[string]json.RawMessage
	} `json:"$defs"`
}

func load(t *testing.T, name string, into any) {
	t.Helper()
	raw, err := os.ReadFile(v1 + name)
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(raw, into); err != nil {
		t.Fatalf("%s: %v", name, err)
	}
}

// jsonFields lists the JSON names a struct encodes, and which are always written.
func jsonFields(v any) (names []string, always []string) {
	typ := reflect.TypeOf(v)
	for i := 0; i < typ.NumField(); i++ {
		name, opts, _ := strings.Cut(typ.Field(i).Tag.Get("json"), ",")
		names = append(names, name)
		if !strings.Contains(opts, "omitempty") {
			always = append(always, name)
		}
	}
	sort.Strings(names)
	sort.Strings(always)
	return names, always
}

func keys[V any](m map[string]V) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

func TestTheTypesHaveTheContractsFields(t *testing.T) {
	var msg schema
	load(t, "message.schema.json", &msg)

	names, always := jsonFields(Message{})
	if want := keys(msg.Properties); !reflect.DeepEqual(names, want) {
		t.Errorf("Message encodes %v, the contract defines %v", names, want)
	}
	// What is always written must be allowed to be: ts is required, seq may be any whole number.
	if !reflect.DeepEqual(always, []string{"seq", "ts"}) {
		t.Errorf("Message always writes %v", always)
	}
	names, always = jsonFields(Position{})
	if want := keys(msg.Defs["position"].Properties); !reflect.DeepEqual(names, want) {
		t.Errorf("Position encodes %v, the contract defines %v", names, want)
	}
	if want := msg.Defs["position"].Required; !reflect.DeepEqual(always, want) {
		t.Errorf("Position always writes %v, the contract requires %v", always, want)
	}
}

func TestTheMetricNameRuleIsTheContracts(t *testing.T) {
	var msg schema
	load(t, "message.schema.json", &msg)
	if got, want := metricName.String(), msg.Properties["metrics"].PropertyNames.Pattern; got != want {
		t.Errorf("metric names: %s here, %s in the contract", got, want)
	}
}

func TestTimestampsAreInTheContractsForm(t *testing.T) {
	var msg schema
	load(t, "message.schema.json", &msg)
	ts := regexp.MustCompile(msg.Properties["ts"].Pattern)
	for _, at := range []time.Time{
		time.Date(2026, 11, 27, 9, 0, 0, 0, time.UTC),                            // a whole second
		time.Date(2026, 11, 27, 9, 0, 0, 123_456_789, time.UTC),                  // more digits than kept
		time.Date(2026, 11, 27, 14, 30, 0, 5_000_000, time.FixedZone("", 19800)), // another zone
		time.Date(9999, 12, 31, 23, 59, 59, 999_000_000, time.UTC),
		{}, // zero: a gateway with no clock at all must still write a valid form
	} {
		if s := Timestamp(at); !ts.MatchString(s) {
			t.Errorf("Timestamp(%v) = %q, not v1's form", at, s)
		}
	}
}

func TestTopicsAreInTheContractsForm(t *testing.T) {
	var topic schema
	load(t, "topic.schema.json", &topic)
	pattern := regexp.MustCompile(topic.Pattern)
	for _, key := range []string{"DRN-0001", "SUB-DRN1-GIMBAL", "CAN-20", "RIG.A-01_x"} {
		if s := Topic("00000000-0000-0000-0000-000000000001", "CommercialDrone", key); !pattern.MatchString(s) {
			t.Errorf("Topic = %q, not v1's form", s)
		}
	}
	// And the contract's own topic cases, through the same pattern.
	var cases struct {
		Topics []struct {
			Topic string
			Valid bool
			Why   string
		}
	}
	load(t, "cases.json", &cases)
	for _, c := range cases.Topics {
		if pattern.MatchString(c.Topic) != c.Valid {
			t.Errorf("topic case %q: the pattern disagrees (%s)", c.Topic, c.Why)
		}
	}
}
