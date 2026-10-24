Tools to make FreeCAD data handling smoother.

## `fc-assy.py`
Assembly dependency manager

## `fc-print`
STL exporter with orientation.

Go implementation; see DETAILS-print.md for the architecture and
DETAILS-watcher.md / DETAILS-body-sameness.md for the design notes.

build & run (from the repository root):
```
go build -o bin/fc-print ./fc-print/cmd/fc-print
./bin/fc-print <watch-dir> <export-dir>
```

* `<watch-dir>` is recursively watched for any files ending with `.FCStd`

Export target:
* bodies having property `Mfg.print` (`Mfg` is group, `print` is property name)

`print` property format
* `print` implies FFF (fused filament fabrication)
  * `key1=value1 key2=value2 ...` (order doesn't matter)
    * e.g. `mat=PLA nozzle=0.2 up=Z+ body=N"`
  * `mat`: `any`, `PLA`, `ASA` etc. Recommended material list, but does not have any semantics.
    * optional (deafult: any)
  * `nozzle`: nozzle diameter
    * optional (default: 0.4)
  * `up`: {X-,X+,Y-,Y+,Z-,Z+}
    * required
  * `body`:
    * optional (default: N)
    * `N`: "normal" setting
    * `S`: "strong" setting
    * `SS`: "strongest" setting (many walls & 100% infill)

export path
* e.g. `PLA0.2N-parts-bracket.stl`
  * format: `<mat><nozzle><body>-<bodyname>(-<number>).stl`
  * orientation (`up`) is applied so that exported stl becomes slicer_up (`Z+`)
  * `<bodyname>` = `<dir1>-<dir2>-...-<bodyname>`
    * directories are relative to `<watch-dir>`.
    * `-<number>` is attached when multiple different bodies results in same `bodyname`. Starts from 2.
    * "-" and " " in `<bodyname>` is converted to "_" to avoid conflict with "-" separator.
    * fc-print guarantees each target body is exported as a single file (no duplicate, no missing).
      * However, number might change in different run of fc-print, or when body configurations are changed
  * Exported files are deleted when original bodies are deleted (either by body gone or file gone)

fc-print only treats `up`. `mat`, `nozzle`, `body` are only used for naming the output file.
