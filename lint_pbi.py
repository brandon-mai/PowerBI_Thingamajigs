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
    def __init__(self, project_path, commit=False, commit_sql=False):
        self.project_path = project_path
        self.commit = commit
        self.commit_sql = commit_sql
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

    def _strip_comments(self, sql):
        # Strip single line comments
        sql = re.sub(r'--.*', '', sql)
        # Strip multi-line comments
        sql = re.sub(r'/\*.*?\*/', '', sql, flags=re.DOTALL)
        return sql

    def _get_sql_fix(self, original_sql, columns, line_content):
        """Standardized Two-Version Parsing Strategy with Comment Stripping."""
        if not columns: return None, None, False
        
        # 1. Analytic Version
        def mask_m_escape(m): return " " * len(m.group(0))
        analytic_sql = re.sub(r'#\([^)]+\)', mask_m_escape, original_sql)
        
        # 2. Scope Detection (Must ignore comments for accurate paren counting)
        # We replace comments with spaces of equal length to maintain indices
        def mask_comment(m): return " " * len(m.group(0))
        scope_sql = re.sub(r'--.*', mask_comment, analytic_sql)
        scope_sql = re.sub(r'/\*.*?\*/', mask_comment, scope_sql, flags=re.DOTALL)
        
        outer_parsing = ""
        depth = 0
        for char in scope_sql:
            if char == '(': depth += 1
            elif char == ')': depth -= 1
            elif depth == 0: outer_parsing += char
            else: outer_parsing += " "
            
        # Catch SELECT * and SELECT alias.*
        star_regex = r'\bSELECT\s+(?:\w+\.)?\*'
        outer_match = re.search(star_regex, outer_parsing, re.IGNORECASE)
        if not outer_match: return None, None, False
        
        # 3. Provider Detection
        is_redshift = any(word in line_content.lower() for word in ["redshift", "odbc", "postgres"])
        def quote(c): return f'"{c}"' if is_redshift else f'[{c}]'
        
        # 4. Deduplication
        start, end = outer_match.span()
        from_match = re.search(r'\bFROM\b', outer_parsing[end:], re.IGNORECASE)
        select_clause_text = ""
        if from_match:
            select_clause_text = analytic_sql[end : end + from_match.start()]
            
        final_cols = []
        for pbi_name, source_name in columns:
            db_name = source_name if source_name else pbi_name
            if re.search(fr'\b{re.escape(db_name)}\b', select_clause_text, re.IGNORECASE):
                continue
            final_cols.append(quote(db_name))
        
        col_list = ", ".join(final_cols)
        highlighted_cols = f"{GREEN}{BOLD}{col_list}{RESET}"
        
        # 5. Join Detection
        has_joins = False
        if from_match:
            after_from = outer_parsing[end + from_match.end():].lower()
            if "," in after_from or " join " in after_from:
                has_joins = True
        
        raw_fixed = original_sql[:start] + f"SELECT {col_list}" + original_sql[end:]
        highlighted_fixed = original_sql[:start] + f"SELECT {highlighted_cols}" + original_sql[end:]
        
        return raw_fixed, highlighted_fixed, not has_joins

    def _lint_tmdl_file(self, file_path):
        filename = os.path.basename(file_path)
        with open(file_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        # Phase 1: Column Inventory
        columns = [] 
        current_col = None
        for line in lines:
            if re.search(r'^\s*(measure|partition|column|table)\b', line) and not line.strip().startswith("column "):
                current_col = None
            if line.strip().startswith("column "):
                c_match = re.search(r'^\s+column\s+\'?([^\'=\r\n]+)\'?', line)
                if c_match and "=" not in line:
                    current_col = c_match.group(1).split('=')[0].strip().strip("'")
                    columns.append([current_col, None])
            elif "sourceColumn:" in line and current_col:
                s_match = re.search(r'sourceColumn:\s*(.*)', line)
                if s_match and columns and columns[-1][0] == current_col:
                    columns[-1][1] = s_match.group(1).strip()

        # Phase 2: Lint
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

            if current_block_type in ["Measure", "Column"]:
                if_div_pattern = r'\bIF\s*\(\s*([^,]+)<>\s*0\s*,\s*([^,]+)\/(\1)\s*,\s*BLANK\(\)\s*\)'
                if re.search(if_div_pattern, line, re.IGNORECASE):
                    highlighted_fix = re.sub(if_div_pattern, f'{GREEN}{BOLD}DIVIDE(\\2, \\1){RESET}', line, flags=re.IGNORECASE).strip()
                    self.log_warning(filename, context, f"IF division detected. Suggest {GREEN}DIVIDE(){RESET}.", highlighted_fix)
                    if self.commit:
                        new_line = re.sub(if_div_pattern, r'DIVIDE(\2, \1)', line, flags=re.IGNORECASE)
                        if new_line != line:
                            file_modified = True
                            self.dax_fixes += 1
                if re.search(r'\bCOUNT\s*\(', line, re.IGNORECASE):
                    self.log_warning(filename, context, f"Found {YELLOW}COUNT(){RESET}. Consider {GREEN}COUNTROWS(){RESET}.")

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
                        raw_fixed, highlighted_fixed, is_safe = self._get_sql_fix(sql_text, columns, line)
                        
                        if highlighted_fixed:
                            display_suggestion = highlighted_fixed
                            if not is_safe:
                                display_suggestion += f"\n        {RED}[!] Warning: Joins detected. Verify aliases.{RESET}"
                            else:
                                display_suggestion += f"\n        {YELLOW}[!] Manual Review Required: Verify sourceColumn names.{RESET}"
                            self.log_warning(filename, context, f"Top-level {YELLOW}'SELECT *'{RESET} detected.", display_suggestion)
                            
                            if self.commit and self.commit_sql and is_safe:
                                new_line = line[:match.start(g_idx)] + raw_fixed + line[match.end(g_idx):]
                                if new_line != line:
                                    file_modified = True
                                    self.sql_fixes += 1
                        
                        # Subquery Check: Now independent of Top-level result
                        clean_sql = self._strip_comments(sql_text.replace('#(lf)', ' '))
                        if re.search(r'\bSELECT\s+(?:\w+\.)?\*', clean_sql, re.IGNORECASE):
                            # Only warn about subquery if it's not the one we just fixed at top level
                            # A simple check: if Top-level fix was NOT found, or if multiple exist
                            star_count = len(re.findall(r'\bSELECT\s+(?:\w+\.)?\*', clean_sql, re.IGNORECASE))
                            if not highlighted_fixed or star_count > 1:
                                self.log_warning(filename, context, f"Subquery {YELLOW}'SELECT *'{RESET} detected. Avoid for better performance.")

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
        if self.commit:
            mode_msg = "DAX fixes applied."
            if self.commit_sql: mode_msg = "DAX and safe SQL fixes applied."
            print(f"{GREEN}[COMMIT MODE]{RESET} {mode_msg}\n")
        else:
            print(f"{YELLOW}[DRY RUN MODE]{RESET} No changes saved. Use {BOLD}--commit{RESET} to fix.\n")
        
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
                    print(f"        {CYAN}Suggestion:{RESET} {w['suggestion'].strip()}\n")
        
        print(f"\n{BOLD}{CYAN}{'='*40}{RESET}")
        print(f"{BOLD}Total Warnings: {len(self.warnings)}{RESET}")
        if self.commit:
            print(f"{BOLD}DAX Fixes Applied: {self.dax_fixes}{RESET}")
            if self.commit_sql:
                print(f"{BOLD}SQL Fixes Applied: {self.sql_fixes}{RESET}")
            else:
                print(f"{YELLOW}SQL Fixes (Use --sql to commit safe fixes){RESET}")
        print()

if __name__ == "__main__":
    if os.name == 'nt': os.system('')
    path = sys.argv[1] if len(sys.argv) > 1 else "."
    commit = "--commit" in sys.argv
    commit_sql = "--sql" in sys.argv
    PBILinter(path, commit=commit, commit_sql=commit_sql).lint()
