
# ===== CONFIG =====
ROOT_PATH = "."              # pasta raiz do projeto
OUTPUT_FILE = "repo_map.txt" # arquivo de saída
INDENT_SPACES = 4            # espaços por nível
IGNORE_DIRS = {".git", "node_modules", "__pycache__", "venv", "release", "dist",}
IGNORE_FILES = set({"repo_map_gen.py"})    
SHOW_HIDDEN = False
# ==================

Path = __import__("pathlib").Path

def write_tree(root_path, out_path):
    root = Path(root_path)

    def safe_iter(path):
        try:
            return sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        except PermissionError:
            return []

    with open(out_path, "w", encoding="utf-8") as f:

        def walk(path, level):
            for entry in safe_iter(path):
                name = entry.name

                if not SHOW_HIDDEN and name.startswith("."):
                    continue

                if entry.is_dir():
                    if name in IGNORE_DIRS:
                        continue
                    f.write(" " * (INDENT_SPACES * level) + name + "/\n")
                    walk(entry, level + 1)
                else:
                    if name in IGNORE_FILES:
                        continue
                    f.write(" " * (INDENT_SPACES * level) + name + "\n")

        if root.is_file():
            if root.name not in IGNORE_FILES:
                f.write(root.name + "\n")
        else:
            f.write(root.name + "/\n")
            walk(root, 1)


if not Path(ROOT_PATH).exists():
    print(f"Caminho não existe: {ROOT_PATH}")
else:
    write_tree(ROOT_PATH, OUTPUT_FILE)
    print(f"Mapa do repositório salvo em: {OUTPUT_FILE}")