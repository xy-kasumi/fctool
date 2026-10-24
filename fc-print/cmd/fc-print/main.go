// Command fc-print keeps a directory of exported STL files in sync with
// the export-target bodies of the .FCStd files under a watched directory.
package main

import (
	"os"

	"fcprint"
)

func main() {
	os.Exit(fcprint.MainCli(os.Args[1:]))
}
