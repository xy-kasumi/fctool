package fcprint

import (
	"context"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"
)

// BodyEventKind distinguishes exports that appeared or changed from those
// whose source body disappeared.
type BodyEventKind string

const (
	BodyChanged BodyEventKind = "changed"
	BodyDeleted BodyEventKind = "deleted"
)

// BodyEvent reports one export-target body transition. OutputPath is nil
// for deleted events.
type BodyEvent struct {
	InputPath  string
	BodyName   string
	Settings   PrintSettings
	BREPHash   string
	OutputPath *string
	ChangedAt  time.Time
	Kind       BodyEventKind
}

// batchQuietWindow is how long the manager waits for further watcher
// events before processing a batch with a single FreeCAD invocation.
const batchQuietWindow = 250 * time.Millisecond

// bodyState is the cached (BREPHash, Settings) and current output of one
// body, used for sameness decisions and export cleanup.
type bodyState struct {
	settings PrintSettings
	brep     string
	output   string
}

// PrintManager keeps exportRoot in sync with the export-target bodies of
// the FCStd files under watchRoot.
type PrintManager struct {
	events       chan BodyEvent
	errs         chan error
	root         string
	exportRoot   string
	freeCAD      FreeCADPaths
	watchOptions WatchOptions

	cancel    context.CancelFunc
	done      chan struct{}
	closeOnce sync.Once

	// statsMu guards the stat* counters below; the body/file counters are
	// refreshed by the run goroutine, the total-file walk runs lazily in
	// Stats on any goroutine.
	statsMu        sync.Mutex
	statBodies     int
	statTaggedFiles int
	statTotalFiles int
	totalCountedAt time.Time

	// run-goroutine state.
	cache      map[string]map[string]bodyState // input path -> body name -> state
	claims     map[string]string               // output path -> claim key
	fileHashes map[string]string               // input path -> content hash
}

// Stats reports the header counters: the number of currently tracked
// bodies, the number of source FCStd files containing valid tags, and the
// total number of FCStd files under the watch root. The total is
// re-counted lazily at most once per watch poll interval; a failed walk
// keeps the previous value.
func (m *PrintManager) Stats() (bodies, tagged, total int) {
	m.statsMu.Lock()
	defer m.statsMu.Unlock()
	if time.Since(m.totalCountedAt) >= m.watchOptions.PollInterval {
		if count, err := countSuffixFiles(m.root, m.watchOptions.FileSuffix); err == nil {
			m.statTotalFiles = count
		}
		m.totalCountedAt = time.Now()
	}
	return m.statBodies, m.statTaggedFiles, m.statTotalFiles
}

// refreshBodyStats recomputes the body and tagged-file counters from the
// cache. Called on the run goroutine after every cache mutation.
func (m *PrintManager) refreshBodyStats() {
	bodies := 0
	for _, fileBodies := range m.cache {
		bodies += len(fileBodies)
	}
	m.statsMu.Lock()
	m.statBodies = bodies
	m.statTaggedFiles = len(m.cache)
	m.statsMu.Unlock()
}

func claimKey(input, name string) string {
	return input + "\x00" + name
}

// claimInput splits a claim key back into its input path.
func claimInput(key string) string {
	if idx := strings.IndexByte(key, 0); idx >= 0 {
		return key[:idx]
	}
	return ""
}

// NewPrintManager starts watching watchRoot. On startup it reconciles
// exportRoot against the current set of target bodies, deleting
// exported STL files whose source bodies no longer exist. Afterwards it
// emits a changed event for every body whose (BREPHash, Settings) differ
// from the cached values, and a deleted event for every body that
// disappears (body removed or file deleted).
func NewPrintManager(
	watchRoot string,
	exportRoot string,
	freeCAD FreeCADPaths,
	watchOptions WatchOptions,
) (*PrintManager, error) {
	absRoot, err := filepath.Abs(watchRoot)
	if err != nil {
		return nil, fmt.Errorf("watch dir: %w", err)
	}
	info, err := os.Stat(absRoot)
	if err != nil {
		return nil, fmt.Errorf("watch dir: %w", err)
	}
	if !info.IsDir() {
		return nil, fmt.Errorf("watch dir %s is not a directory", absRoot)
	}
	absExport, err := filepath.Abs(exportRoot)
	if err != nil {
		return nil, fmt.Errorf("export dir: %w", err)
	}
	if err := os.MkdirAll(absExport, 0o755); err != nil {
		return nil, fmt.Errorf("export dir: %w", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	manager := &PrintManager{
		events:       make(chan BodyEvent, 128),
		errs:         make(chan error, 16),
		root:         absRoot,
		exportRoot:   absExport,
		freeCAD:      freeCAD,
		watchOptions: normalizeWatchOptions(watchOptions),
		cancel:       cancel,
		done:         make(chan struct{}),
		cache:        make(map[string]map[string]bodyState),
		claims:       make(map[string]string),
		fileHashes:   make(map[string]string),
	}
	go manager.run(ctx)
	return manager, nil
}

// Events returns the body change stream. The channel closes on Close.
func (m *PrintManager) Events() <-chan BodyEvent { return m.events }

// Errors returns non-fatal operational errors. The channel closes on
// Close; a *DangerousRoot error ends the manager.
func (m *PrintManager) Errors() <-chan error { return m.errs }

// Close stops watching and waits for the manager goroutine.
func (m *PrintManager) Close() error {
	m.closeOnce.Do(func() {
		m.cancel()
		<-m.done
	})
	return nil
}

func (m *PrintManager) run(ctx context.Context) {
	defer close(m.done)
	defer close(m.events)
	defer close(m.errs)
	if !m.initialPass(ctx) {
		return
	}
	paths, errs := WatchDirectory(ctx, m.root, m.watchOptions)
	m.watchLoop(ctx, paths, errs)
}

func (m *PrintManager) emit(ctx context.Context, event BodyEvent) bool {
	select {
	case m.events <- event:
		return true
	case <-ctx.Done():
		return false
	}
}

func (m *PrintManager) sendErr(ctx context.Context, err error) {
	select {
	case m.errs <- err:
	case <-ctx.Done():
	}
}

func sleepCtx(ctx context.Context, d time.Duration) bool {
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-timer.C:
		return true
	}
}

func sortedSetKeys(set map[string]bool) []string {
	keys := make([]string, 0, len(set))
	for key := range set {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	return keys
}

// initialPass scans the whole watch root once, extracts the current set of
// target bodies, emits a changed event for each, and reconciles exportRoot
// (deleting STL files whose source bodies no longer exist). Source files
// that could not be processed at all are retried until they succeed.
func (m *PrintManager) initialPass(ctx context.Context) bool {
	snapshot, _, err := scanWithRetry(
		m.root, m.watchOptions.FileSuffix,
		m.watchOptions.InitialEntryLimit, true)
	if err != nil {
		if ctx.Err() != nil {
			return false
		}
		m.sendErr(ctx, err)
		var dangerous *DangerousRoot
		return !errors.As(err, &dangerous)
	}
	hashes := snapshot // input path -> content hash
	remaining := sortedSetKeys(mapFromKeys(hashes))
	for {
		if len(remaining) == 0 {
			m.reconcile(ctx)
			return ctx.Err() == nil
		}
		failed, ok := m.extractRound(ctx, remaining, hashes)
		if !ok {
			return false
		}
		if len(failed) == 0 {
			m.reconcile(ctx)
			return ctx.Err() == nil
		}
		if !sleepCtx(ctx, 5*time.Second) {
			return false
		}
		remaining = failed
	}
}

func mapFromKeys(hashes map[string]string) map[string]bool {
	set := make(map[string]bool, len(hashes))
	for path := range hashes {
		set[path] = true
	}
	return set
}

// extractRound runs one headless FreeCAD invocation over paths and merges
// the results into the cache, emitting change events. Bodies whose output
// name collided with another file's claim cause the victim's input to be
// re-extracted in a follow-up round (so no body's export is lost). Returns
// the source paths that failed at file level and need retrying.
func (m *PrintManager) extractRound(
	ctx context.Context,
	paths []string,
	hashes map[string]string,
) ([]string, bool) {
	for round := 0; ; round++ {
		if len(paths) == 0 {
			return nil, true
		}
		result, err := ExtractBodies(ctx, m.freeCAD, ExtractBodiesRequest{
			SourcePaths: paths,
			OutputDir:   m.exportRoot,
		})
		if ctx.Err() != nil {
			return nil, false
		}
		var extractErr *ExtractError
		if err != nil && !errors.As(err, &extractErr) {
			// Hard failure: nothing usable; caller retries after a delay.
			m.sendErr(ctx, err)
			return paths, true
		}
		skip := map[string]bool{}
		if extractErr != nil {
			for _, input := range extractErr.FailedInputs {
				skip[input] = true
			}
		}
		byInput := make(map[string][]ExtractedBody)
		for _, body := range result.Bodies {
			if skip[body.InputPath] {
				continue
			}
			byInput[body.InputPath] = append(byInput[body.InputPath], body)
		}
		requeue := map[string]bool{}
		for _, input := range sortedBodyInputs(byInput) {
			victims, ok := m.mergeFile(ctx, input, byInput[input])
			if !ok {
				return nil, false
			}
			if hash, exists := hashes[input]; exists {
				m.fileHashes[input] = hash
			}
			for victim := range victims {
				requeue[victim] = true
			}
		}
		if err != nil {
			m.sendErr(ctx, err)
		}
		if len(requeue) == 0 {
			var failed []string
			if extractErr != nil {
				failed = extractErr.FailedInputs
			}
			return failed, true
		}
		if round >= 3 {
			m.sendErr(ctx, fmt.Errorf(
				"output name collisions did not converge for %s",
				strings.Join(sortedSetKeys(requeue), ", ")))
			return nil, true
		}
		paths = sortedSetKeys(requeue)
	}
}

func sortedBodyInputs(byInput map[string][]ExtractedBody) []string {
	inputs := make([]string, 0, len(byInput))
	for input := range byInput {
		inputs = append(inputs, input)
	}
	sort.Strings(inputs)
	return inputs
}

// mergeFile diffs the freshly extracted bodies of one input file against
// the cache: emits changed events for new or (BREPHash, Settings)-changed
// bodies and deleted events for bodies that vanished from the file.
// Output-name claims are adjusted so that a body's export can never be
// silently overwritten by a body from a different file. Returns the input
// paths of collision victims whose exports were clobbered and need
// re-extraction.
func (m *PrintManager) mergeFile(
	ctx context.Context,
	input string,
	bodies []ExtractedBody,
) (map[string]bool, bool) {
	current := make(map[string]ExtractedBody, len(bodies))
	for _, body := range bodies {
		current[body.BodyName] = body
	}
	prev := m.cache[input]
	if prev == nil {
		prev = make(map[string]bodyState)
	}
	victims := map[string]bool{}
	now := time.Now()

	// Deletions first, so removed bodies release their output names for
	// the additions below.
	for name, state := range prev {
		if _, ok := current[name]; !ok {
			if !m.emit(ctx, BodyEvent{
				InputPath: input, BodyName: name, Settings: state.settings,
				BREPHash: state.brep, OutputPath: nil, ChangedAt: now,
				Kind: BodyDeleted,
			}) {
				return nil, false
			}
			m.releaseClaim(input, name, state.output)
			_ = os.Remove(state.output)
			delete(prev, name)
		}
	}

	for _, body := range bodies {
		target, victim, err := m.claimOutputPath(input, body.BodyName, body.OutputPath)
		if err != nil {
			m.sendErr(ctx, fmt.Errorf("%s: %s: %w", input, body.BodyName, err))
			continue
		}
		if victim != "" {
			victims[victim] = true
		}
		state := bodyState{settings: body.Settings, brep: body.BREPHash, output: target}
		if old, existed := prev[body.BodyName]; !existed || old != state {
			output := target
			if !m.emit(ctx, BodyEvent{
				InputPath: input, BodyName: body.BodyName, Settings: body.Settings,
				BREPHash: body.BREPHash, OutputPath: &output, ChangedAt: now,
				Kind: BodyChanged,
			}) {
				return nil, false
			}
		}
		prev[body.BodyName] = state
	}
	if len(prev) == 0 {
		delete(m.cache, input)
	} else {
		m.cache[input] = prev
	}
	m.refreshBodyStats()
	return victims, true
}

// releaseClaim drops the output-name reservation of (input, name) and
// removes its file.
func (m *PrintManager) releaseClaim(input, name, output string) {
	if m.claims[output] == claimKey(input, name) {
		delete(m.claims, output)
	}
}

// claimOutputPath reserves the freshly written STL path for one body. If
// the path is already claimed by a body of another file (the collision
// numbering of a single-file extraction differs from a full-tree run),
// the file is moved to a free "-2"/"-3"/... variant and the previous
// owner's input is reported so its export can be rewritten.
func (m *PrintManager) claimOutputPath(input, name, extracted string) (string, string, error) {
	key := claimKey(input, name)
	target := filepath.Clean(extracted)
	if old, existed := m.cache[input][name]; existed {
		previous := old.output
		if m.claims[previous] == key {
			delete(m.claims, previous)
		}
		if previous != target && m.claims[previous] == "" {
			_ = os.Remove(previous) // stale file from a previous numbering
		}
	}
	victim := ""
	if owner, claimed := m.claims[target]; claimed && owner != key {
		free, err := m.freeOutputPath(target)
		if err != nil {
			return "", "", err
		}
		if err := os.Rename(target, free); err != nil {
			return "", "", fmt.Errorf("move colliding export: %w", err)
		}
		victim = claimInput(owner)
		target = free
	}
	m.claims[target] = key
	return target, victim, nil
}

// freeOutputPath finds the first unclaimed "-2"/"-3"/... variant of taken.
func (m *PrintManager) freeOutputPath(taken string) (string, error) {
	ext := filepath.Ext(taken)
	stem := taken[:len(taken)-len(ext)]
	for n := 2; n < 1000; n++ {
		candidate := fmt.Sprintf("%s-%d%s", stem, n, ext)
		if _, claimed := m.claims[candidate]; claimed {
			continue
		}
		if _, err := os.Stat(candidate); err == nil {
			continue
		} else if !errors.Is(err, fs.ErrNotExist) {
			return "", err
		}
		return candidate, nil
	}
	return "", fmt.Errorf("no free output name near %s", taken)
}

// deleteFileBodies drops every cached body of a deleted input file,
// removing its exports.
func (m *PrintManager) deleteFileBodies(ctx context.Context, input string) bool {
	delete(m.fileHashes, input)
	prev := m.cache[input]
	if prev == nil {
		return true
	}
	now := time.Now()
	for name, state := range prev {
		if !m.emit(ctx, BodyEvent{
			InputPath: input, BodyName: name, Settings: state.settings,
			BREPHash: state.brep, OutputPath: nil, ChangedAt: now,
			Kind: BodyDeleted,
		}) {
			return false
		}
		m.releaseClaim(input, name, state.output)
		_ = os.Remove(state.output)
	}
	delete(m.cache, input)
	m.refreshBodyStats()
	return true
}

// reconcile deletes every STL file under exportRoot that is not claimed by
// a live target body.
func (m *PrintManager) reconcile(ctx context.Context) {
	keep := make(map[string]bool, len(m.claims))
	for path := range m.claims {
		keep[path] = true
	}
	_ = filepath.WalkDir(m.exportRoot, func(path string, entry fs.DirEntry, err error) error {
		if err != nil {
			return nil // unreadable entry: never delete what we cannot inspect
		}
		if ctx.Err() != nil {
			return ctx.Err()
		}
		if entry.IsDir() || !strings.EqualFold(filepath.Ext(path), ".stl") {
			return nil
		}
		if !keep[filepath.Clean(path)] {
			_ = os.Remove(path)
		}
		return nil
	})
}

// watchLoop consumes watcher events, coalescing bursts into batches.
func (m *PrintManager) watchLoop(ctx context.Context, paths <-chan string, errs <-chan error) {
	for {
		select {
		case <-ctx.Done():
			return
		case err, ok := <-errs:
			if !ok {
				return
			}
			m.sendErr(ctx, err)
			var dangerous *DangerousRoot
			if errors.As(err, &dangerous) {
				return
			}
		case path, ok := <-paths:
			if !ok {
				return
			}
			batch := []string{path}
			timer := time.NewTimer(batchQuietWindow)
		collect:
			for len(batch) < 1024 {
				select {
				case path, ok := <-paths:
					if !ok {
						break collect
					}
					batch = append(batch, path)
				case err, ok := <-errs:
					if !ok {
						break collect
					}
					m.sendErr(ctx, err)
					var dangerous *DangerousRoot
					if errors.As(err, &dangerous) {
						timer.Stop()
						return
					}
				case <-timer.C:
					break collect
				case <-ctx.Done():
					timer.Stop()
					return
				}
			}
			timer.Stop()
			if !m.handleBatch(ctx, batch) {
				return
			}
		}
	}
}

// handleBatch classifies watcher events: disappeared paths delete their
// bodies' exports, changed paths (by content hash) are re-extracted.
func (m *PrintManager) handleBatch(ctx context.Context, batch []string) bool {
	hashes := map[string]string{}
	remaining := []string{}
	for _, path := range batch {
		info, err := os.Stat(path)
		if err == nil && info.IsDir() {
			continue
		}
		if err != nil && !errors.Is(err, fs.ErrNotExist) {
			m.sendErr(ctx, fmt.Errorf("%s: %w", path, err))
			continue
		}
		if err != nil {
			if !m.deleteFileBodies(ctx, path) {
				return false
			}
			continue
		}
		digest, err := hashFile(path)
		if err != nil {
			if errors.Is(err, fs.ErrNotExist) {
				if !m.deleteFileBodies(ctx, path) {
					return false
				}
				continue
			}
			m.sendErr(ctx, fmt.Errorf("%s: %w", path, err))
			continue
		}
		if m.fileHashes[path] == digest {
			continue
		}
		hashes[path] = digest
		remaining = append(remaining, path)
	}
	for attempt := 0; ; attempt++ {
		failed, ok := m.extractRound(ctx, remaining, hashes)
		if !ok {
			return false
		}
		if len(failed) == 0 || attempt >= 2 {
			return true
		}
		if !sleepCtx(ctx, 2*time.Second) {
			return false
		}
		remaining = failed
	}
}
