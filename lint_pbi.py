import os
import re
import sys

# ANSI Colors for terminal output
YELLOW = "\033[93m"
RED = "\033[91m"
GREEN = "\033[92m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"

class PBILinter:
    def __init__(self, project_path, commit=False):
        self.project_path = project_path
        self.commit = commit
        self.semantic_model_dir = self._find_semantic_model(project_path)
        self.warnings = []
        self.fixes_made = 0

    def _find_semantic_model(self, path):
        if not os.path.exists(path): return None
        if path.endswith(".SemanticModel"): return path
        if os.path.isdir(path):
            for d in os.listdir(path):
                full_path = os.path.join(path, d)
                if d.endswith(".SemanticModel") and os.path.isdir(full_path):
                    return full_path
        return None

    def log_warning(self, file, context, message, suggestion=None):
        self.warnings.append({
            "file": file,
            "context": context,
            "message": message,
            "suggestion": suggestion
        })

    def lint(self):
        if not self.semantic_model_dir:
            print(f"{RED}[ERROR]{RESET} Could not find .SemanticModel directory in {self.project_path}")
            return

        self._lint_tables()
        self._lint_relationships()
        self._print_report()

    def _lint_tables(self):
        tables_path = os.path.join(self.semantic_model_dir, "definition", "tables")
        if not os.path.exists(tables_path): return

        for filename in os.listdir(tables_path):
            if filename.endswith(".tmdl"):
                self._lint_tmdl_file(os.path.join(tables_path, filename))

    def _suggest_sql_fix(self, sql_text, columns):
        """Replaces the LAST 'SELECT *' with the provided column list."""
        if not columns: return None
        col_list = ", ".join([f"[{c}]" for c in columns])
        # Find all SELECT * occurrences
        matches = list(re.finditer(r'\bSELECT\s+\*', sql_text, re.IGNORECASE))
        if not matches: return None
        
        # Target the last one (heuristic for the final output select)
        last_match = matches[-1]
        start, end = last_match.span()
        return sql_text[:start] + f"SELECT {col_list}" + sql_text[end:]

    def _lint_tmdl_file(self, file_path):
        filename = os.path.basename(file_path)
        with open(file_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        # Phase 1: Inventory the columns in this file
        columns = []
        for line in lines:
            if line.strip().startswith("column "):
                c_match = re.search(r'^\s+column\s+\'?([^\'=\r\n]+)\'?', line)
                if c_match:
                    col_name = c_match.group(1).split('=')[0].strip().strip("'")
                    # Exclude calculated columns from SQL source inventory
                    if "=" not in line:
                        columns.append(col_name)

        # Phase 2: Perform Linting and potential auto-fixes
        new_lines = []
        modified = False
        current_block_type = "Table"
        current_block_name = filename.replace(".tmdl", "")
        
        for line in lines:
            # Block Tracking
            measure_match = re.search(r'^\s*measure\s+\'?([^\'=\r\n]+)\'?\s*=', line)
            column_match = re.search(r'^\s+column\s+\'?([^\'=\r\n]+)\'?', line)
            partition_match = re.search(r'^\s+partition\s+\'?([^\'=\r\n]+)\'?\s*=', line)
            
            if measure_match:
                current_block_type = "Measure"
                current_block_name = measure_match.group(1).strip().strip("'")
            elif column_match and line.strip().startswith("column "):
                current_block_type = "Column"
                current_block_name = column_match.group(1).split('=')[0].strip().strip("'")
            elif partition_match:
                current_block_type = "Partition"
                current_block_name = partition_match.group(1).strip().strip("'")

            context = f"{current_block_type} [{current_block_name}]"
            new_line = line

            # --- DAX Rules ---
            if current_block_type in ["Measure", "Column"]:
                # Rule: IF Division
                if re.search(r'\bIF\s*\(.*<>\s*0.*\/', line, re.IGNORECASE):
                    self.log_warning(filename, context, f"Potential {YELLOW}IF division{RESET}. Use {GREEN}DIVIDE(){RESET}.")
                
                # Rule: COUNT
                if re.search(r'\bCOUNT\s*\(', line, re.IGNORECASE):
                    self.log_warning(filename, context, f"Found {YELLOW}COUNT(){RESET}. Consider {GREEN}COUNTROWS(){RESET}.")

            # --- SQL Rules ---
            if current_block_type == "Partition":
                # Capture SQL strings in common Power Query functions
                sql_patterns = [
                    r'Query\s*=\s*"([^"]+)"',
                    r'Odbc\.Query\s*\([^,]+,\s*"([^"]+)"',
                    r'Value\.NativeQuery\s*\([^,]+,\s*"([^"]+)"'
                ]
                for pattern in sql_patterns:
                    match = re.search(pattern, line, re.IGNORECASE)
                    if match:
                        sql_text = match.group(1)
                        if re.search(r'SELECT\s+\*', sql_text, re.IGNORECASE):
                            suggestion = self._suggest_sql_fix(sql_text, columns)
                            self.log_warning(filename, context, f"SQL {YELLOW}'SELECT *'{RESET} detected.", suggestion)

            new_lines.append(new_line)

        # Apply changes if in commit mode
        if modified and self.commit:
            with open(file_path, "w", encoding="utf-8") as f:
                f.writelines(new_lines)

    def _lint_relationships(self):
        rel_path = os.path.join(self.semantic_model_dir, "definition", "relationships.tmdl")
        if not os.path.exists(rel_path): return
        with open(rel_path, "r", encoding="utf-8") as f:
            content = f.read()
        if "crossFilteringBehavior: both" in content:
            self.log_warning("relationships.tmdl", "Relationship", "Bi-directional filter detected.")
        if "fromCardinality: many" in content and "toCardinality: many" in content:
            self.log_warning("relationships.tmdl", "Relationship", "Many-to-Many cardinality detected.")

    def _print_report(self):
        print(f"\n{BOLD}{CYAN}=== POWER BI LINT REPORT ==={RESET}")
        print(f"{BOLD}Project:{RESET} {os.path.basename(self.semantic_model_dir)}")
        if self.commit: print(f"{GREEN}[COMMIT MODE]{RESET} Applying auto-fixes where safe.\n")
        else: print(f"{YELLOW}[DRY RUN MODE]{RESET} No changes will be saved. Use {BOLD}--commit{RESET} to fix.\n")
        
        if not self.warnings:
            print(f"{GREEN}[OK]{RESET} No issues found!")
        else:
            self.warnings.sort(key=lambda x: (x['file'], x['context']))
            last_file = None
            for w in self.warnings:
                if w['file'] != last_file:
                    print(f"\n{BOLD}{CYAN}# {w['file']}{RESET}")
                    last_file = w['file']
                print(f"  {YELLOW}[WRN]{RESET} {BOLD}{w['context']}{RESET}: {w['message']}")
                if w['suggestion']:
                    print(f"        {GREEN}Suggested Fix:{RESET}")
                    print(f"        {w['suggestion'].strip()}\n")
        
        print(f"\n{BOLD}{CYAN}{'='*40}{RESET}")
        print(f"{BOLD}Total Warnings: {len(self.warnings)}{RESET}")
        if self.commit: print(f"{BOLD}Auto-fixes Applied: {self.fixes_made}{RESET}")
        print()

if __name__ == "__main__":
    if os.name == 'nt': os.system('')
    path = sys.argv[1] if len(sys.argv) > 1 else "."
    commit = "--commit" in sys.argv
    PBILinter(path, commit=commit).lint()
