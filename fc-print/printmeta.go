package fcprint

import (
	"fmt"
	"strings"
)

// Direction is the Mfg.print axis that must point up (slicer Z+) in the
// exported STL.
type Direction string

const (
	UpXMinus Direction = "X-"
	UpXPlus  Direction = "X+"
	UpYMinus Direction = "Y-"
	UpYPlus  Direction = "Y+"
	UpZMinus Direction = "Z-"
	UpZPlus  Direction = "Z+"
)

// BodyMode selects the intended slicing strength preset. fc-print itself
// only reads it; it is used for naming the exported STL.
type BodyMode string

const (
	BodyNormal    BodyMode = "N"
	BodyStrong    BodyMode = "S"
	BodyStrongest BodyMode = "SS"
)

// PrintSettings is the parsed Mfg.print property of an export-target body.
// fc-print only applies Up; Mat, Nozzle and Body shape the output file name.
type PrintSettings struct {
	Up     Direction
	Mat    string
	Nozzle string
	Body   BodyMode
}

func validDirection(value string) bool {
	switch Direction(value) {
	case UpXMinus, UpXPlus, UpYMinus, UpYPlus, UpZMinus, UpZPlus:
		return true
	}
	return false
}

func validBodyMode(value string) bool {
	switch BodyMode(value) {
	case BodyNormal, BodyStrong, BodyStrongest:
		return true
	}
	return false
}

// isASCIISpace reports whether b is ASCII whitespace (the EBNF's WSP).
func isASCIISpace(b byte) bool {
	switch b {
	case ' ', '\t', '\n', '\v', '\f', '\r':
		return true
	}
	return false
}

// asciiFields splits value on runs of ASCII whitespace (the EBNF's
// separator). Non-ASCII whitespace such as U+00A0 stays inside a value.
func asciiFields(value string) []string {
	var fields []string
	start := -1
	for i := 0; i <= len(value); i++ {
		if i == len(value) || isASCIISpace(value[i]) {
			if start >= 0 {
				fields = append(fields, value[start:i])
				start = -1
			}
		} else if start < 0 {
			start = i
		}
	}
	return fields
}

// ParsePrintSettings parses Mfg.print using this EBNF:
//
//	settings  = field, { separator, field };
//	field     = "mat=", value | "nozzle=", value
//	          | "up=", direction | "body=", body;
//	direction = "X-" | "X+" | "Y-" | "Y+" | "Z-" | "Z+";
//	body      = "N" | "S" | "SS";
//	separator = WSP, { WSP };
//	value     = NONWSP, { NONWSP };
//
// WSP is ASCII whitespace; NONWSP excludes WSP and '='. Keys are
// case-sensitive, unique, and unordered. up is required; defaults are
// mat=any, nozzle=0.4, and body=N. There is no quoting or escaping.
func ParsePrintSettings(value string) (PrintSettings, error) {
	settings := PrintSettings{Mat: "any", Nozzle: "0.4", Body: BodyNormal}
	seen := make(map[string]bool, 4)
	for _, field := range asciiFields(value) {
		eq := strings.IndexByte(field, '=')
		if eq <= 0 {
			return settings, fmt.Errorf("print settings %q: invalid field %q", value, field)
		}
		key, fieldValue := field[:eq], field[eq+1:]
		if fieldValue == "" || strings.IndexByte(fieldValue, '=') >= 0 {
			return settings, fmt.Errorf("print settings %q: invalid value in %q", value, field)
		}
		switch key {
		case "mat", "nozzle":
			// Free-form values.
		case "up":
			if !validDirection(fieldValue) {
				return settings, fmt.Errorf("print settings %q: invalid up direction %q", value, fieldValue)
			}
		case "body":
			if !validBodyMode(fieldValue) {
				return settings, fmt.Errorf("print settings %q: invalid body mode %q", value, fieldValue)
			}
		default:
			return settings, fmt.Errorf("print settings %q: unknown key %q", value, key)
		}
		if seen[key] {
			return settings, fmt.Errorf("print settings %q: duplicate key %q", value, key)
		}
		seen[key] = true
		switch key {
		case "mat":
			settings.Mat = fieldValue
		case "nozzle":
			settings.Nozzle = fieldValue
		case "up":
			settings.Up = Direction(fieldValue)
		case "body":
			settings.Body = BodyMode(fieldValue)
		}
	}
	if !seen["up"] {
		return settings, fmt.Errorf("print settings %q: missing required up", value)
	}
	return settings, nil
}
