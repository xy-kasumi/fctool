package fcprint

import (
	"context"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"syscall"
	"time"
)

// MainCli implements the fc-print command line:
//
//	fc-print [flags] <watch-dir> <export-dir>
//
// It recursively watches <watch-dir> for .FCStd files, exports every body
// with a Mfg.print property into <export-dir> as an oriented STL, and
// keeps the exports in sync as files change. It returns the process exit
// code.
func MainCli(args []string) int {
	flags := flag.NewFlagSet("fc-print", flag.ContinueOnError)
	flags.SetOutput(os.Stderr)
	freecadPath := flags.String("freecad", "",
		"FreeCADCmd `path` (default: $FC_PRINT_FREECAD, $FREECAD_CMD, then auto-detect)")
	pollInterval := flags.Duration("poll", 2*time.Second,
		"watch poll `interval`")
	entryLimit := flags.Int("limit", 10000,
		"refuse to watch roots with more directory `entries` than this on the first scan")
	flags.Usage = func() {
		fmt.Fprintln(flags.Output(), "usage: fc-print [flags] <watch-dir> <export-dir>")
		fmt.Fprintln(flags.Output())
		fmt.Fprintln(flags.Output(), "  Watches <watch-dir> recursively for .FCStd files and keeps <export-dir>")
		fmt.Fprintln(flags.Output(), "  in sync: every body with a Mfg.print property is reoriented (up -> Z+)")
		fmt.Fprintln(flags.Output(), "  and exported to one STL file; exports are deleted when their bodies")
		fmt.Fprintln(flags.Output(), "  disappear. TUI keys: c clears the list, q quits.")
		fmt.Fprintln(flags.Output())
		flags.PrintDefaults()
	}
	if err := flags.Parse(args); err != nil {
		return 2
	}
	if flags.NArg() != 2 {
		flags.Usage()
		return 2
	}
	watchRoot, exportRoot := flags.Arg(0), flags.Arg(1)

	freeCAD, err := ResolveFreeCADPaths(*freecadPath)
	if err != nil {
		fmt.Fprintln(os.Stderr, "fc-print:", err)
		return 1
	}
	manager, err := NewPrintManager(watchRoot, exportRoot, freeCAD, WatchOptions{
		PollInterval:      *pollInterval,
		InitialEntryLimit: *entryLimit,
		FileSuffix:        ".FCStd",
	})
	if err != nil {
		fmt.Fprintln(os.Stderr, "fc-print:", err)
		return 1
	}
	defer manager.Close()

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	if err := RunTUI(ctx, manager); err != nil {
		fmt.Fprintln(os.Stderr, "fc-print:", err)
		manager.Close()
		return 1
	}
	return 0
}
