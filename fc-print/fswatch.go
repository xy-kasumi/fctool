package fcprint

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

// WatchOptions configures WatchDirectory.
type WatchOptions struct {
	PollInterval      time.Duration
	InitialEntryLimit int
	FileSuffix        string
}

// DangerousRoot is returned when the first scan of root exceeds
// options.InitialEntryLimit directory entries, indicating the root is
// too large to poll. See DETAILS-watcher.md.
type DangerousRoot struct {
	EntryCount int
}

func (e *DangerousRoot) Error() string {
	return fmt.Sprintf(
		"watch root is too large to poll: %d directory entries on the first scan exceeded the entry limit",
		e.EntryCount)
}

// errScanRetry marks a scan that raced with concurrent directory removal;
// the caller retries instead of interpreting the race as deletions.
var errScanRetry = errors.New("directory vanished during scan")

func normalizeWatchOptions(options WatchOptions) WatchOptions {
	if options.PollInterval <= 0 {
		options.PollInterval = 2 * time.Second
	}
	if options.InitialEntryLimit <= 0 {
		options.InitialEntryLimit = 10000
	}
	if options.FileSuffix == "" {
		options.FileSuffix = ".FCStd"
	}
	return options
}

// hashFile returns the SHA-256 of the file's contents.
func hashFile(path string) (string, error) {
	file, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer file.Close()
	digest := sha256.New()
	if _, err := io.Copy(digest, file); err != nil {
		return "", err
	}
	return hex.EncodeToString(digest.Sum(nil)), nil
}

func matchesSuffix(name, suffix string) bool {
	return len(name) >= len(suffix) &&
		strings.EqualFold(name[len(name)-len(suffix):], suffix)
}

// scanTree walks root iteratively (explicit directory stack,
// follow_symlinks=false) and returns the set of files matching suffix
// mapped to their content hashes, plus the number of directory entries
// visited. Memory is proportional to the matching paths, not to all
// entries. When enforceLimit is set, the scan aborts with *DangerousRoot
// once more than limit entries have been visited.
func scanTree(root, suffix string, limit int, enforceLimit bool) (map[string]string, int, error) {
	snapshot := make(map[string]string)
	dirs := []string{root}
	entries := 0
	for len(dirs) > 0 {
		dir := dirs[len(dirs)-1]
		dirs = dirs[:len(dirs)-1]
		handle, err := os.Open(dir)
		if err != nil {
			if errors.Is(err, fs.ErrNotExist) {
				return nil, entries, errScanRetry
			}
			return nil, entries, err
		}
		list, err := handle.ReadDir(-1)
		handle.Close()
		if err != nil {
			if errors.Is(err, fs.ErrNotExist) {
				return nil, entries, errScanRetry
			}
			return nil, entries, err
		}
		for _, entry := range list {
			entries++
			if enforceLimit && entries > limit {
				return nil, entries, &DangerousRoot{EntryCount: entries}
			}
			if entry.Type()&os.ModeSymlink != 0 {
				continue
			}
			name := entry.Name()
			if entry.IsDir() {
				dirs = append(dirs, filepath.Join(dir, name))
				continue
			}
			if !matchesSuffix(name, suffix) {
				continue
			}
			path := filepath.Join(dir, name)
			digest, err := hashFile(path)
			if err != nil {
				if errors.Is(err, fs.ErrNotExist) {
					continue // raced with removal; not a deletion signal
				}
				return nil, entries, err
			}
			snapshot[path] = digest
		}
	}
	return snapshot, entries, nil
}

// scanWithRetry retries scans that raced with concurrent directory removal,
// then gives up with a surfaced error rather than risking missed events.
func scanWithRetry(root, suffix string, limit int, enforceLimit bool) (map[string]string, int, error) {
	for attempt := 0; ; attempt++ {
		snapshot, entries, err := scanTree(root, suffix, limit, enforceLimit)
		if !errors.Is(err, errScanRetry) {
			return snapshot, entries, err
		}
		if attempt >= 2 {
			return nil, 0, fmt.Errorf("%s: kept changing during scan", root)
		}
	}
}

// WatchDirectory polls root (recursive, no symlink following) and, after
// each successful complete scan, emits every extant path matching
// FileSuffix plus every path of the previous snapshot that has since
// disappeared. Suppression is by file content hash only, never by
// (mtime, size); a rename therefore emits the new path, not a delete.
// Scan failures are surfaced on the error channel instead of being
// interpreted as unreadable subtrees; a first scan that exceeds
// InitialEntryLimit yields *DangerousRoot and ends the watch.
func WatchDirectory(
	ctx context.Context,
	root string,
	options WatchOptions,
) (<-chan string, <-chan error) {
	options = normalizeWatchOptions(options)
	paths := make(chan string, 64)
	errs := make(chan error, 4)
	go func() {
		defer close(paths)
		defer close(errs)
		first := true
		var previous map[string]string
		for {
			snapshot, _, err := scanWithRetry(
				root, options.FileSuffix, options.InitialEntryLimit, first)
			if err != nil {
				select {
				case errs <- err:
				case <-ctx.Done():
					return
				}
				var dangerous *DangerousRoot
				if errors.As(err, &dangerous) {
					return
				}
			} else {
				first = false
				if !emitScanDiff(ctx, paths, previous, snapshot) {
					return
				}
				previous = snapshot
			}
			select {
			case <-ctx.Done():
				return
			case <-time.After(options.PollInterval):
			}
		}
	}()
	return paths, errs
}

// emitScanDiff sends new and content-changed paths followed by
// disappeared paths. Reports false when ctx is done first.
func emitScanDiff(ctx context.Context, paths chan<- string, previous, current map[string]string) bool {
	changed := make([]string, 0, len(current))
	for path, digest := range current {
		if previous == nil || previous[path] != digest {
			changed = append(changed, path)
		}
	}
	sort.Strings(changed)
	for _, path := range changed {
		select {
		case paths <- path:
		case <-ctx.Done():
			return false
		}
	}
	if previous != nil {
		gone := make([]string, 0)
		for path := range previous {
			if _, ok := current[path]; !ok {
				gone = append(gone, path)
			}
		}
		sort.Strings(gone)
		for _, path := range gone {
			select {
			case paths <- path:
			case <-ctx.Done():
				return false
			}
		}
	}
	return true
}
