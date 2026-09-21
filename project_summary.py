import os
from pathlib import Path

# --- CONFIGURATION ---
# Add or remove folders, files, and extensions to ignore
IGNORE_DIRS = {
    'venv', '.venv', 'env', '.git', '__pycache__', '.pytest_cache', 
    'log', 'logs', 'cache', 'database', 'db', 'node_modules', 'data'
}

IGNORE_FILES = {
    '.DS_Store', 'thumbs.db', 'project_summary.py, project_summary.md'
}

IGNORE_EXTENSIONS = {
    # Databases & Spreadsheets
    '.db', '.sqlite', '.sqlite3', '.xlsx', '.xls', '.csv', '.ods',
    # Logs
    '.log',
    # Binary / Executables / Images
    '.pyc', '.exe', '.png', '.jpg', '.jpeg', '.gif', '.pdf', '.zip', '.tar', '.gz'
}

OUTPUT_FILE = "project_summary.md"
# ---------------------

def should_ignore(path: Path) -> bool:
    """Check if a file or directory should be ignored."""
    # Check directory parts
    for part in path.parts:
        if part in IGNORE_DIRS:
            return True
    
    # Check files and extensions
    if path.is_file():
        if path.name in IGNORE_FILES:
            return True
        if path.suffix.lower() in IGNORE_EXTENSIONS:
            return True
            
    return False

def generate_tree(dir_path: Path, prefix: str = "") -> str:
    """Recursively build a visual text-based directory tree."""
    tree_str = ""
    
    # Get sorted list of items that are not ignored
    try:
        items = sorted([item for item in dir_path.iterdir() if not should_ignore(item)],
                       key=lambda x: (x.is_file(), x.name.lower()))
    except PermissionError:
        return ""

    pointers = [r"├── "] * (len(items) - 1) + [r"└── "] if items else []
    
    for pointer, item in zip(pointers, items):
        if item.is_dir():
            tree_str += f"{prefix}{pointer}{item.name}/\n"
            extension = "│   " if pointer == r"├── " else "    "
            tree_str += generate_tree(item, prefix + extension)
        else:
            tree_str += f"{prefix}{pointer}{item.name}\n"
            
    return tree_str

def get_all_files(dir_path: Path) -> list:
    """Recursively get all files that are not ignored."""
    file_list = []
    try:
        for item in sorted(dir_path.rglob('*')):
            if item.is_file() and not should_ignore(item):
                file_list.append(item)
    except PermissionError:
        pass
    return file_list

def main():
    project_root = Path.cwd()
    
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as markdown_file:
        # 1. Write Header
        markdown_file.write(f"# Project Structure and Files: {project_root.name}\n\n")
        
        # 2. Write Directory Tree
        markdown_file.write("## Directory Tree\n")
        markdown_file.write("```text\n")
        markdown_file.write(f"{project_root.name}/\n")
        markdown_file.write(generate_tree(project_root))
        markdown_file.write("```\n\n")
        
        # 3. Write File Contents
        markdown_file.write("## File Contents\n\n")
        
        valid_files = get_all_files(project_root)
        
        for file_path in valid_files:
            # Skip the output file itself if it's in the same directory
            if file_path.name == OUTPUT_FILE:
                continue
                
            # Get relative path for clean display headers
            relative_path = file_path.relative_to(project_root)
            markdown_file.write(f"### File: `{relative_path}`\n\n")
            
            # Determine code block syntax highlighting based on file extension
            lang = file_path.suffix.lstrip('.') if file_path.suffix else ""
            markdown_file.write(f"```{lang}\n")
            
            # Read and write content safely
            try:
                with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
                    markdown_file.write(f.read())
            except Exception as e:
                markdown_file.write(f"[Error reading file: {str(e)}]\n")
                
            markdown_file.write("\n```\n\n")
            
    print(f"Successfully generated {OUTPUT_FILE}!")

if __name__ == "__main__":
    main()
