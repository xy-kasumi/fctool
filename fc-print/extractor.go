package fcprint

import (
	"context"
	_ "embed"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

//go:embed extract_core.py
var extractCorePy []byte

// ExtractBodiesRequest asks for every export-target body of SourcePaths to
// be exported into OutputDir.
type ExtractBodiesRequest struct {
	SourcePaths []string
	OutputDir   string
}

// ExtractedBody describes one exported body. BREPHash is the SHA-256 of
// the loaded body's exportBrepToString(), before reorientation (see
// DETAILS-body-sameness.md).
type ExtractedBody struct {
	BodyName   string
	Settings   PrintSettings
	BREPHash   string
	OutputPath string
	InputPath  string
}

// ExtractBodiesResult collects the bodies of one invocation.
type ExtractBodiesResult struct {
	Bodies []ExtractedBody
}

// ExtractError reports per-entry failures of an ExtractBodies invocation.
// FailedInputs lists source documents that could not be processed at all
// (e.g. unreadable files); per-body problems such as malformed Mfg.print
// strings appear in Errors without listing their input in FailedInputs, so
// callers may treat those bodies as no longer being export targets.
type ExtractError struct {
	FailedInputs []string
	Errors       []error
}

func (e *ExtractError) Error() string {
	parts := make([]string, len(e.Errors))
	for i, err := range e.Errors {
		parts[i] = err.Error()
	}
	return strings.Join(parts, "; ")
}

type extractPayload struct {
	Bodies []extractedBodyJSON `json:"bodies"`
	Errors []extractErrorJSON  `json:"errors"`
}

type extractedBodyJSON struct {
	Source   int          `json:"source"`
	Name     string       `json:"name"`
	Label    string       `json:"label"`
	Settings settingsJSON `json:"settings"`
	BREP     string       `json:"brep"`
	Output   string       `json:"output"`
}

type settingsJSON struct {
	Up     string `json:"up"`
	Mat    string `json:"mat"`
	Nozzle string `json:"nozzle"`
	Body   string `json:"body"`
}

type extractErrorJSON struct {
	Source int    `json:"source"`
	Body   string `json:"body"`
	Error  string `json:"error"`
}

// ExtractBodies opens every FCStd file in SourcePaths with a single
// headless FreeCAD invocation, extracts every body having a Mfg.print
// property, reorients it so that the settings' up axis becomes Z+, and
// exports each body to exactly one STL file under OutputDir. On output
// name collisions, "-2", "-3", ... are appended.
func ExtractBodies(
	ctx context.Context,
	freeCAD FreeCADPaths,
	request ExtractBodiesRequest,
) (ExtractBodiesResult, error) {
	result := ExtractBodiesResult{}
	if len(request.SourcePaths) == 0 {
		return result, nil
	}
	if err := os.MkdirAll(request.OutputDir, 0o755); err != nil {
		return result, fmt.Errorf("extract bodies: create output dir: %w", err)
	}
	resultsPath := filepath.Join(request.OutputDir, ".fc-print-extract.json")
	_ = os.Remove(resultsPath) // never trust stale results from a failed run
	args := make([]string, 0, len(request.SourcePaths)+2)
	args = append(args, request.OutputDir, resultsPath)
	args = append(args, request.SourcePaths...)
	if err := InvokeFreeCAD(ctx, freeCAD, extractCorePy, args...); err != nil {
		return result, fmt.Errorf("extract bodies from %d file(s): %w",
			len(request.SourcePaths), err)
	}
	defer os.Remove(resultsPath)

	data, err := os.ReadFile(resultsPath)
	if err != nil {
		return result, fmt.Errorf("extract bodies: read results: %w", err)
	}
	var payload extractPayload
	if err := json.Unmarshal(data, &payload); err != nil {
		return result, fmt.Errorf("extract bodies: parse results: %w", err)
	}

	// The child process reports source indexes and output file names
	// (never translated paths); rejoin them with the host-side paths.
	sourcePath := func(index int) string {
		if index >= 0 && index < len(request.SourcePaths) {
			return request.SourcePaths[index]
		}
		return fmt.Sprintf("<bad source index %d>", index)
	}
	extractionErr := &ExtractError{}
	failed := map[string]bool{}
	for _, failure := range payload.Errors {
		input := sourcePath(failure.Source)
		extractionErr.Errors = append(extractionErr.Errors,
			fmt.Errorf("%s: %s", input, failure.Error))
		if failure.Body == "" && !failed[input] {
			failed[input] = true
			extractionErr.FailedInputs = append(extractionErr.FailedInputs, input)
		}
	}
	for _, body := range payload.Bodies {
		result.Bodies = append(result.Bodies, ExtractedBody{
			BodyName:   body.Name,
			Settings:   PrintSettings{Up: Direction(body.Settings.Up), Mat: body.Settings.Mat, Nozzle: body.Settings.Nozzle, Body: BodyMode(body.Settings.Body)},
			BREPHash:   body.BREP,
			OutputPath: filepath.Join(request.OutputDir, body.Output),
			InputPath:  sourcePath(body.Source),
		})
	}
	if len(extractionErr.Errors) == 0 {
		return result, nil
	}
	return result, extractionErr
}
