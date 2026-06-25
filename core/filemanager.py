import json
import os

from core.exceptions import InvalidJSONException, FileNotFoundException


class FileManager:
    """Provides methods for file and directory management."""

    # Per-world data directory holding config.json + cache/. None means "use the
    # project root", so single-world setups stay byte-for-byte unchanged. Set by
    # twb.py (FileManager.set_data_dir) when started with --world <name>.
    _data_dir = None

    # Shared *code* resources that always live with the source tree, never inside
    # a per-world data dir (so every world reuses the same templates / example).
    _CODE_PATHS = ("config.example.json", "templates")

    @staticmethod
    def get_root():
        """Returns the root directory of the project (source tree)."""
        return os.path.join(os.path.dirname(__file__), "..")

    @staticmethod
    def set_data_dir(path):
        """Point config.json + cache/ at a per-world data directory.

        Pass None (the default) to use the project root, i.e. single-world mode.
        """
        FileManager._data_dir = path

    @staticmethod
    def get_data_root():
        """Where per-world data (config.json, cache/) lives."""
        return FileManager._data_dir or FileManager.get_root()

    @staticmethod
    def _is_code_path(path):
        """True for shared code resources that ignore the per-world data dir."""
        norm = str(path).replace("\\", "/")
        if norm.startswith("./"):
            norm = norm[2:]
        return any(norm == c or norm.startswith(c + "/") for c in FileManager._CODE_PATHS)

    @staticmethod
    def _resolve(path):
        """Resolve a relative path against the code root or per-world data root.

        Code resources (templates/, config.example.json) always resolve against
        the project root; everything else (config.json, cache/) resolves against
        the active data dir. Absolute paths are returned unchanged.
        """
        base = FileManager.get_root() if FileManager._is_code_path(path) else FileManager.get_data_root()
        return os.path.join(base, path)

    @staticmethod
    def get_path(path):
        """Returns the full path of a file or directory."""
        return FileManager._resolve(path)

    @staticmethod
    def path_exists(path):
        """Returns True if the path exists, False otherwise.

        Resolves relative paths against the code/data root like the other
        methods (absolute paths pass through unchanged), so an existence check
        and the matching read/write always agree on which file they mean.
        """
        return os.path.exists(FileManager._resolve(path))

    @staticmethod
    def create_directory(directory):
        """Creates a directory if it does not exist."""
        if not os.path.exists(directory):
            os.makedirs(directory)

    @staticmethod
    def create_directories(directories):
        """Creates a list of directories (resolved against the data root)."""
        for directory in directories:
            FileManager.create_directory(FileManager._resolve(directory))

    @staticmethod
    def list_directory(directory, ends_with=None):
        """Returns a list of files in a directory. If ends_with is specified, only files ending with the specified
        string will be returned."""
        full_path = FileManager._resolve(directory)
        files = os.listdir(full_path)
        if ends_with:
            files = [f for f in files if f.endswith(ends_with)]
        return files

    @staticmethod
    def __open_file(path, mode="r"):
        """Opens a file in the specified mode. Private do NOT use outside filemanager."""
        full_path = FileManager._resolve(path)
        try:
            return open(full_path, mode)
        except:
            raise FileNotFoundException

    @staticmethod
    def read_file(path):
        """Reads the contents of a file and returns the data. Returns None if the file does not exist."""
        full_path = FileManager._resolve(path)

        if not FileManager.path_exists(full_path):
            return None

        with FileManager.__open_file(full_path) as file:
            return file.read()

    @staticmethod
    def read_lines(path):
        """Reads the contents of a file and returns the lines. Returns None if the file does not exist."""
        full_path = FileManager._resolve(path)

        if not FileManager.path_exists(full_path):
            return None

        with FileManager.__open_file(full_path) as file:
            return file.readlines()

    @staticmethod
    def remove_file(path):
        """Removes a file if it exists."""
        full_path = FileManager._resolve(path)

        if FileManager.path_exists(full_path):
            os.remove(full_path)

    @staticmethod
    def load_json_file(path, **kwargs):
        """Loads a JSON file and returns the data. Returns None if the file does not exist."""
        full_path = FileManager._resolve(path)

        if not FileManager.path_exists(full_path):
            return None

        with FileManager.__open_file(full_path) as file:
            try:
                return json.load(file, **kwargs)
            except json.decoder.JSONDecodeError:
                raise InvalidJSONException

    @staticmethod
    def save_json_file(data, path, **kwargs):
        """Saves data to a JSON file. If the file does not exist, it will be created."""
        full_path = FileManager._resolve(path)

        with FileManager.__open_file(full_path, mode="w") as file:
            json.dump(data, file, indent=2, sort_keys=False, **kwargs)

    @staticmethod
    def save_json_file_atomic(data, path, **kwargs):
        """Like save_json_file, but writes to a temp file and renames it in place.

        os.replace is atomic, so a concurrent reader (e.g. the incoming poller
        loading cache/session.json while the main loop refreshes it) always sees
        either the old or the new file, never a half-written one.
        """
        full_path = FileManager._resolve(path)
        tmp_path = "%s.%d.tmp" % (full_path, os.getpid())
        with open(tmp_path, "w") as file:
            json.dump(data, file, indent=2, sort_keys=False, **kwargs)
        os.replace(tmp_path, full_path)

    @staticmethod
    def copy_file(src_path, dest_path):
        """Copies a file from the source path to the destination path."""
        full_src_path = FileManager._resolve(src_path)
        full_dest_path = FileManager._resolve(dest_path)

        if not FileManager.path_exists(full_src_path):
            return False

        with FileManager.__open_file(full_src_path) as src_file:
            with FileManager.__open_file(full_dest_path, mode="w") as dest_file:
                dest_file.write(src_file.read())
