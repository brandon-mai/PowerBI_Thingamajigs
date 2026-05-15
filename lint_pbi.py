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
        self.dax_fixes = 0
        self.sql_fixes = 0

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

    def _get_sql_fix(self, sql_text, columns, line_content):
        """Returns (fixed_sql, is_safe_to_commit)"""
        if not columns: return None, False
        
        # Detect quoting style
        is_redshift = any(word in line_content.lower() for word in ["redshift", "odbc", "postgres"])
        def quote(c): return f'"{c}"' if is_redshift else f'[{c}]'
        
        # Use the sourceColumn mapping if available
        # Columns is a list of (pbi_name, source_name)
        col_list = ", ".join([quote(source_name if source_name else pbi_name) for pbi_name, source_name in columns])
        
        matches = list(re.finditer(r'\bSELECT\s+\*', sql_text, re.IGNORECASE))
        if not matches: return None, False
        
        last_match = matches[-1]
        remaining = sql_text[last_match.end():].lower()
        has_joins = " join " in remaining or "," in remaining
        
        fixed = sql_text[:last_match.start()] + f"SELECT {col_list}" + sql_text[last_match.end():]
        return fixed, not has_joins

    def _lint_tmdl_file(self, file_path):
        filename = os.path.basename(file_path)
        with open(file_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        # Phase 1: Inventory columns with their source mapping
        columns = [] # List of (pbi_name, source_name)
        current_col = None
        for line in lines:
            if line.strip().startswith("column "):
                c_match = re.search(r'^\s+column\s+\'?([^\'=\r\n]+)\'?', line)
                if c_match and "=" not in line:
                    current_col = c_match.group(1).split('=')[0].strip().strip("'")
                    columns.append([current_col, None])
            elif "sourceColumn:" in line and current_col:
                s_match = re.search(r'sourceColumn:\s*(.*)', line)
                if s_match:
                    columns[-1][1] = s_match.group(1).strip()

        # Phase 2: Lint and Fix
        new_lines = []
        file_modified = False
        current_block_type = "Table"
        current_block_name = filename.replace(".tmdl", "")
        
        for line in lines:
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
                if_div_pattern = r'\bIF\s*\(\s*([^,]+)<>\s*0\s*,\s*([^,]+)\/(\1)\s*,\s*BLANK\(\)\s*\)'
                if re.search(if_div_pattern, line, re.IGNORECASE):
                    fix_expr = re.sub(if_div_pattern, r'DIVIDE(\2, \1)', line, flags=re.IGNORECASE).strip()
                    self.log_warning(filename, context, f"IF division detected. Suggest {GREEN}DIVIDE(){RESET}.", fix_expr)
                    if self.commit:
                        new_line = re.sub(if_div_pattern, r'DIVIDE(\2, \1)', line, flags=re.IGNORECASE)
                        if new_line != line:
                            file_modified = True
                            self.dax_fixes += 1
                
                if re.search(r'\bCOUNT\s*\(', line, re.IGNORECASE):
                    self.log_warning(filename, context, f"Found {YELLOW}COUNT(){RESET}. Consider {GREEN}COUNTROWS(){RESET}.")

            # --- SQL Rules ---
            if current_block_type == "Partition":
                sql_patterns = [
                    (r'(Query\s*=\s*")([^"]+)(")', 2),
                    (r'(Odbc\.Query\s*\([^,]+,\s*")([^"]+)(")', 2),
                    (r'(Value\.NativeQuery\s*\([^,]+,\s*")([^"]+)(")', 2)
                ]
                for pattern, g_idx in sql_patterns:
                    match = re.search(pattern, line, re.IGNORECASE)
                    if match:
                        sql_text = match.group(g_idx)
                        if re.search(r'SELECT\s+\*', sql_text, re.IGNORECASE):
                            fixed_sql, is_safe = self._get_sql_fix(sql_text, columns, line)
                            display_suggestion = fixed_sql
                            if not is_safe:
                                display_suggestion += f"\n        {RED}[!] Warning: Joins detected. Verify aliases.{RESET}"
                            else:
                                display_suggestion += f"\n        {YELLOW}[!] Manual Review Required: Verify sourceColumn names.{RESET}"
                            
                            self.log_warning(filename, context, f"SQL {YELLOW}'SELECT *'{RESET} detected.", display_suggestion)
                            # SQL AUTO-FIX DISABLED FOR SAFETY
                            # if self.commit and is_safe: ...

            new_lines.append(new_line)

        if file_modified and self.commit:
            with open(file_path, "w", encoding="utf-8") as f:
                f.writelines(new_lines)

    def _lint_relationships(self):
        rel_path = os.path.join(self.semantic_model_dir, "definition", "relationships.tmdl")
        if not os.path.exists(rel_path): return
        with open(rel_path, "r", encoding="utf-8") as f: content = f.read()
        if "crossFilteringBehavior: both" in content: self.log_warning("relationships.tmdl", "Relationship", "Bi-directional filter.")
        if "fromCardinality: many" in content and "toCardinality: many" in content: self.log_warning("relationships.tmdl", "Relationship", "Many-to-Many cardinality.")

    def _print_report(self):
        print(f"\n{BOLD}{CYAN}=== POWER BI LINT REPORT ==={RESET}")
        print(f"{BOLD}Project:{RESET} {os.path.basename(self.semantic_model_dir)}")
        if self.commit: print(f"{GREEN}[COMMIT MODE]{RESET} Applying safe auto-fixes to DAX.\n")
        else: print(f"{YELLOW}[DRY RUN MODE]{RESET} No changes saved. Use {BOLD}--commit{RESET} to fix.\n")
        
        if not self.warnings: print(f"{GREEN}[OK]{RESET} No issues found!")
        else:
            self.warnings.sort(key=lambda x: (x['file'], x['context']))
            last_file = None
            for w in self.warnings:
                if w['file'] != last_file:
                    print(f"\n{BOLD}{CYAN}# {w['file']}{RESET}")
                    last_file = w['file']
                print(f"  {YELLOW}[WRN]{RESET} {BOLD}{w['context']}{RESET}: {w['message']}")
                if w['suggestion']:
                    print(f"        {GREEN}Suggestion:{RESET} {w['suggestion'].strip()}\n")
        
        print(f"\n{BOLD}{CYAN}{'='*40}{RESET}")
        print(f"{BOLD}Total Warnings: {len(self.warnings)}{RESET}")
        if self.commit:
            print(f"{BOLD}DAX Fixes Applied: {self.dax_fixes}{RESET}")
            print(f"{YELLOW}SQL Fixes (Auto-commit disabled for safety){RESET}")
        print()

if __name__ == "__main__":
    if os.name == 'nt': os.system('')
    path = sys.argv[1] if len(sys.argv) > 1 else "."
    commit = "--commit" in sys.argv
    PBILinter(path, commit=commit).lint()
