package fcprint

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
)

// FreeCADPaths names the FreeCADCmd executable used for headless work.
// FreeCAD is never imported into the host process (see fcstd_to_stl.py).
type FreeCADPaths struct {
	ExecutablePath string
}

// FreeCADFailure describes a FreeCADCmd child process that could not be
// spawned or exited with a non-zero status.
type FreeCADFailure struct {
	Command  []string
	ExitCode int
	Stdout   string
	Stderr   string
}

func (e *FreeCADFailure) Error() string {
	detail := strings.TrimSpace(e.Stderr)
	if detail == "" {
		detail = strings.TrimSpace(e.Stdout)
	}
	if runes := []rune(detail); len(runes) > 400 {
		detail = string(runes[:400]) + "..."
	}
	command := ""
	if len(e.Command) > 0 {
		command = strings.Join(e.Command, " ")
	}
	return fmt.Sprintf("freecad failed with exit code %d: %s [%s]", e.ExitCode, detail, command)
}

// isWSL reports whether this is Linux running under the Windows Subsystem
// for Linux.
func isWSL() bool {
	if runtime.GOOS != "linux" {
		return false
	}
	release, err := os.ReadFile("/proc/sys/kernel/osrelease")
	if err != nil {
		return false
	}
	return strings.Contains(strings.ToLower(string(release)), "microsoft")
}

// invokesWindowsFreeCAD reports whether executablePath refers to a Windows
// FreeCADCmd.exe to be run through WSL interop (in which case every path
// argument must be translated with wslpath).
func invokesWindowsFreeCAD(executablePath string) bool {
	if !isWSL() {
		return false
	}
	return strings.HasSuffix(strings.ToLower(executablePath), ".exe") ||
		strings.HasPrefix(executablePath, "/mnt/")
}

// wslToWindowsPath translates a native (WSL) path into the Windows form
// expected by a Windows child process. Falls back to the input on failure.
func wslToWindowsPath(path string) string {
	if path == "" {
		return path
	}
	out, err := exec.Command("wslpath", "-w", path).Output()
	if err != nil {
		return path
	}
	return strings.TrimSpace(string(out))
}

// writeTempScript writes pythonSource to a temporary file, preferably in
// the directory of args[0] so that a Windows FreeCADCmd.exe invoked via
// WSL interop can read it without crossing the 9P boundary.
func writeTempScript(pythonSource []byte, args []string) (string, func(), error) {
	candidateDirs := []string{}
	if len(args) > 0 {
		candidateDirs = append(candidateDirs, filepath.Dir(args[0]))
	}
	candidateDirs = append(candidateDirs, "")
	var file *os.File
	var err error
	for _, dir := range candidateDirs {
		file, err = os.CreateTemp(dir, ".fc-print-core-*.py")
		if err == nil {
			break
		}
	}
	if err != nil {
		return "", nil, fmt.Errorf("create script file: %w", err)
	}
	path := file.Name()
	cleanup := func() { _ = os.Remove(path) }
	if _, err := file.Write(pythonSource); err != nil {
		file.Close()
		cleanup()
		return "", nil, fmt.Errorf("write script file: %w", err)
	}
	if err := file.Close(); err != nil {
		cleanup()
		return "", nil, fmt.Errorf("close script file: %w", err)
	}
	return path, cleanup, nil
}

// InvokeFreeCAD executes FreeCADCmd (paths.ExecutablePath) as a child
// process, running pythonSource with args appended as sys.argv[2:].
// FreeCAD is never imported into the host process (see fcstd_to_stl.py).
// On WSL, a Windows FreeCADCmd.exe is invoked via interop and every path
// argument is translated with wslpath. A spawn failure or a non-zero
// exit code yields a *FreeCADFailure.
func InvokeFreeCAD(
	ctx context.Context,
	paths FreeCADPaths,
	pythonSource []byte,
	args ...string,
) error {
	if paths.ExecutablePath == "" {
		return errors.New("freecad: no executable path configured")
	}
	scriptPath, cleanup, err := writeTempScript(pythonSource, args)
	if err != nil {
		return fmt.Errorf("freecad: %w", err)
	}
	defer cleanup()

	translate := func(p string) string { return p }
	if invokesWindowsFreeCAD(paths.ExecutablePath) {
		translate = wslToWindowsPath
	}
	cmdArgs := []string{translate(scriptPath)}
	for _, arg := range args {
		cmdArgs = append(cmdArgs, translate(arg))
	}

	cmd := exec.CommandContext(ctx, paths.ExecutablePath, cmdArgs...)
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	runErr := cmd.Run()
	if ctx.Err() != nil {
		return ctx.Err()
	}
	if runErr == nil {
		return nil
	}
	failure := &FreeCADFailure{
		Command: append([]string{paths.ExecutablePath}, cmdArgs...),
		Stdout:  stdout.String(),
		Stderr:  stderr.String(),
	}
	var exitErr *exec.ExitError
	if errors.As(runErr, &exitErr) {
		failure.ExitCode = exitErr.ExitCode()
	} else {
		// Spawn failure (e.g. executable not found).
		failure.ExitCode = -1
		failure.Stderr = strings.TrimSpace(failure.Stderr + "\n" + runErr.Error())
	}
	return failure
}

// defaultFreeCADCandidates lists FreeCADCmd executables to try when no
// explicit path or environment override is configured.
func defaultFreeCADCandidates() []string {
	var candidates []string
	if isWSL() {
		// The Windows binary may be cased "freecadcmd.exe" or
		// "FreeCADCmd.exe"; glob is case-sensitive, so list the bin
		// directory and match case-insensitively.
		installs, _ := filepath.Glob("/mnt/c/Program Files/FreeCAD*/bin")
		var exes []string
		for _, bin := range installs {
			entries, err := os.ReadDir(bin)
			if err != nil {
				continue
			}
			for _, entry := range entries {
				if !entry.IsDir() && strings.EqualFold(entry.Name(), "FreeCADCmd.exe") {
					exes = append(exes, filepath.Join(bin, entry.Name()))
				}
			}
		}
		sortDescByVersion(exes)
		candidates = append(candidates, exes...)
	}
	candidates = append(candidates, "FreeCADCmd", "freecadcmd")
	return candidates
}

// sortDescByVersion sorts glob matches so that newer-looking versions come
// first (FreeCAD 1.1 before 1.0 before 0.20).
func sortDescByVersion(paths []string) {
	version := func(path string) int {
		// .../FreeCAD 1.1/bin/FreeCADCmd.exe -> "FreeCAD 1.1"
		return versionDigits(filepath.Base(filepath.Dir(filepath.Dir(path))))
	}
	sort.Slice(paths, func(i, j int) bool {
		vi, vj := version(paths[i]), version(paths[j])
		if vi != vj {
			return vi > vj
		}
		return paths[i] > paths[j]
	})
}

// versionDigits extracts the leading "x.y" from a directory name like
// "FreeCAD 1.1", returning a comparable integer (e.g. 110).
func versionDigits(name string) int {
	start := -1
	for i := 0; i < len(name); i++ {
		if name[i] >= '0' && name[i] <= '9' {
			start = i
			break
		}
	}
	if start < 0 {
		return 0
	}
	value := 0
	seenDot := false
	for i := start; i < len(name); i++ {
		switch {
		case name[i] >= '0' && name[i] <= '9':
			value = value*10 + int(name[i]-'0')
		case name[i] == '.' && !seenDot:
			seenDot = true
		default:
			return value
		}
	}
	return value
}

// ResolveFreeCADPaths locates FreeCADCmd. Precedence: the explicit
// argument, $FC_PRINT_FREECAD, $FREECAD_CMD, then platform defaults
// (a Windows install via WSL interop, or FreeCADCmd on PATH).
func ResolveFreeCADPaths(explicit string) (FreeCADPaths, error) {
	candidates := []string{}
	for _, candidate := range []string{
		explicit,
		os.Getenv("FC_PRINT_FREECAD"),
		os.Getenv("FREECAD_CMD"),
	} {
		if candidate != "" {
			candidates = append(candidates, candidate)
		}
	}
	candidates = append(candidates, defaultFreeCADCandidates()...)
	for _, candidate := range candidates {
		if info, err := os.Stat(candidate); err == nil && !info.IsDir() {
			return FreeCADPaths{ExecutablePath: candidate}, nil
		}
		if path, err := exec.LookPath(candidate); err == nil {
			return FreeCADPaths{ExecutablePath: path}, nil
		}
	}
	return FreeCADPaths{}, errors.New("FreeCADCmd not found; pass -freecad <path> or set FREECAD_CMD")
}
