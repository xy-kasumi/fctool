package fcprint

import (
	"bytes"
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

// RunTUI renders the manager's body events as a list of bodies with last
// changed timestamps, ordered most-recent-first. Key bindings: c clears
// the list (shown in the UI), q or Ctrl-C quits. Falls back to plain
// line output when stdin/stdout is not a terminal.
func RunTUI(
	ctx context.Context,
	manager *PrintManager,
) error {
	events := manager.Events()
	errs := manager.Errors()
	if !isCharDevice(os.Stdin) || !isCharDevice(os.Stdout) {
		return runPlainOutput(ctx, events, errs)
	}
	restore, err := enableRawMode()
	if err != nil {
		return runPlainOutput(ctx, events, errs)
	}
	defer restore()

	fmt.Print("\x1b[?1049h\x1b[?25l") // alternate screen, hide cursor
	defer fmt.Print("\x1b[?25h\x1b[?1049l")

	keys := make(chan byte, 16)
	go func() {
		defer close(keys)
		buffer := make([]byte, 32)
		for {
			n, err := os.Stdin.Read(buffer)
			if err != nil || n == 0 {
				return
			}
			select {
			case keys <- buffer[0]:
			default:
			}
		}
	}()

	entries := []BodyEvent{}
	status := ""
	ticker := time.NewTicker(500 * time.Millisecond)
	defer ticker.Stop()
	draw(entries, status)
	for {
		select {
		case <-ctx.Done():
			return nil
		case event, ok := <-events:
			if !ok {
				return nil
			}
			entries = append([]BodyEvent{event}, entries...)
			if len(entries) > 1000 {
				entries = entries[:1000]
			}
			draw(entries, status)
		case err, ok := <-errs:
			if !ok {
				return nil
			}
			status = err.Error()
			draw(entries, status)
		case key, ok := <-keys:
			if !ok {
				return nil
			}
			switch key {
			case 'c', 'C':
				entries = nil
				status = ""
				draw(entries, status)
			case 'q', 'Q', 0x03: // q or Ctrl-C
				return nil
			}
		case <-ticker.C:
			draw(entries, status)
		}
	}
}

func runPlainOutput(ctx context.Context, events <-chan BodyEvent, errs <-chan error) error {
	for {
		select {
		case <-ctx.Done():
			return nil
		case event, ok := <-events:
			if !ok {
				return nil
			}
			fmt.Printf("%s %s %s:%s up=%s mat=%s nozzle=%s body=%s\n",
				event.ChangedAt.Format(time.Stamp), event.Kind,
				filepath.Base(event.InputPath), event.BodyName,
				event.Settings.Up, event.Settings.Mat,
				event.Settings.Nozzle, event.Settings.Body)
		case err, ok := <-errs:
			if !ok {
				return nil
			}
			fmt.Fprintln(os.Stderr, "error:", err)
		}
	}
}

func draw(entries []BodyEvent, status string) {
	width, height := termSize()
	var out strings.Builder
	out.WriteString("\x1b[H\x1b[0J") // home, clear below
	line := func(format string, args ...any) {
		text := []rune(fmt.Sprintf(format, args...))
		if len(text) > width {
			text = text[:width]
		}
		out.WriteString(string(text))
		out.WriteString("\x1b[K\r\n")
	}
	line("fc-print  (c: clear  q: quit)")
	line("%s", strings.Repeat("-", minInt(width, 72)))
	max := height - 4
	if max > 0 && len(entries) > max {
		line("... %d older", len(entries)-max)
		entries = entries[:max]
	}
	for _, event := range entries {
		mark := "+"
		if event.Kind == BodyDeleted {
			mark = "x"
		}
		line("%s %s %-11s %s:%s up=%s mat=%s nozzle=%s body=%s",
			event.ChangedAt.Format("15:04:05"), mark,
			event.Kind,
			filepath.Base(event.InputPath), event.BodyName,
			event.Settings.Up, event.Settings.Mat,
			event.Settings.Nozzle, event.Settings.Body)
	}
	if status != "" {
		line("err: %s", status)
	}
	_, _ = os.Stdout.WriteString(out.String())
}

func minInt(a, b int) int {
	if a < b {
		return a
	}
	return b
}

func isCharDevice(file *os.File) bool {
	info, err := file.Stat()
	if err != nil {
		return false
	}
	return info.Mode()&os.ModeCharDevice != 0
}

func stty(args ...string) (string, error) {
	cmd := exec.Command("stty", args...)
	cmd.Stdin = os.Stdin
	var out bytes.Buffer
	cmd.Stdout = &out
	err := cmd.Run()
	return out.String(), err
}

// enableRawMode switches the terminal to raw mode and returns a restore
// function. Unavailable on non-Unix platforms; callers should fall back.
func enableRawMode() (func(), error) {
	saved, err := stty("-g")
	if err != nil {
		return nil, err
	}
	if _, err := stty("raw", "-echo"); err != nil {
		return nil, err
	}
	return func() { _, _ = stty(strings.TrimSpace(saved)) }, nil
}

// termSize returns the terminal width and height via stty, with a
// 80x24 fallback.
func termSize() (int, int) {
	out, err := stty("size")
	if err != nil {
		return 80, 24
	}
	fields := strings.Fields(out)
	if len(fields) != 2 {
		return 80, 24
	}
	rows, errRows := strconv.Atoi(fields[0])
	cols, errCols := strconv.Atoi(fields[1])
	if errRows != nil || errCols != nil || rows <= 0 || cols <= 0 {
		return 80, 24
	}
	return cols, rows
}
