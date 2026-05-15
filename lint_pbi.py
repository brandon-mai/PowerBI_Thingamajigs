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
    def __init__(self, project_path):
        self.project_path = project_path
        self.semantic_model_dir = self._find_semantic_model(project_path)
        self.warnings = []

    def _find_semantic_model(self, path):
        if not os.path.exists(path): return None
        if path.endswith(".SemanticModel"): return path
        if os.path.isdir(path):
            for d in os.listdir(path):
                full_path = os.path.join(path, d)
                if d.endswith(".SemanticModel") and os.path.isdir(full_path):
                    return full_path
        return None

    def log_warning(self, file, context, message):
        self.warnings.append({
            "file": file,
            "context": context,
            "message": message
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

    def _lint_tmdl_file(self, file_path):
        filename = os.path.basename(file_path)
        with open(file_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        current_block_type = "Table"
        current_block_name = filename.replace(".tmdl", "")
        
        for line in lines:
            # Block Detection: Updates current context as we move through the file
            measure_match = re.search(r'^\s*measure\s+\'?([^\'=\r\n]+)\'?\s*=', line)
            column_match = re.search(r'^\s+column\s+\'?([^\'=\r\n]+)\'?', line)
            partition_match = re.search(r'^\s+partition\s+\'?([^\'=\r\n]+)\'?\s*=', line)
            
            if measure_match:
                current_block_type = "Measure"
                current_block_name = measure_match.group(1).strip().strip("'")
            elif column_match:
                # Basic column detection (avoiding summarizeBy or other sub-properties)
                if line.strip().startswith("column "):
                    current_block_type = "Column"
                    current_block_name = column_match.group(1).split('=')[0].strip().strip("'")
            elif partition_match:
                current_block_type = "Partition"
                current_block_name = partition_match.group(1).strip().strip("'")

            context = f"{current_block_type} [{current_block_name}]"
            
            # --- DAX RULES (Applied to Measures and Calculated Columns) ---
            if current_block_type in ["Measure", "Column"]:
                # Rule 1: IF Division
                # We use \b boundary and check for the division slash
                if re.search(r'\bIF\s*\(.*<>\s*0.*\/', line, re.IGNORECASE):
                    self.log_warning(filename, context, f"Potential {YELLOW}'IF' division{RESET}. Use {GREEN}DIVIDE(){RESET} for safer execution.")
                
                # Rule 2: COUNT (Word boundary avoids DISTINCTCOUNT)
                if re.search(r'\bCOUNT\s*\(', line, re.IGNORECASE):
                    self.log_warning(filename, context, f"Function {YELLOW}COUNT(){RESET} used. Consider {GREEN}COUNTROWS(){RESET} for better performance.")

                # Rule 3: FILTER(Table) heuristic
                filter_match = re.search(r'\bFILTER\s*\(\s*(\'?[a-zA-Z0-9 ]+\'?)\s*,', line)
                if filter_match:
                    table_ref = filter_match.group(1)
                    # Ignore if it's already wrapped in a columnar function like ALL or VALUES
                    if '[' not in table_ref and '(' not in table_ref and 'ALL' not in table_ref:
                        self.log_warning(filename, context, f"FILTER() applied directly to table '{table_ref}'. Use column-specific filters instead.")

            # --- SQL RULES (Applied to Partitions) ---
            if current_block_type == "Partition":
                # Patterns to catch SQL strings after Query= or in Odbc/Value functions
                sql_patterns = [
                    r'Query\s*=\s*"([^"]+)"',
                    r'Odbc\.Query\s*\([^,]+,\s*"([^"]+)"',
                    r'Value\.NativeQuery\s*\([^,]+,\s*"([^"]+)"'
                ]
                
                for pattern in sql_patterns:
                    for sql_match in re.finditer(pattern, line, re.IGNORECASE):
                        sql_text = sql_match.group(1)
                        if re.search(r'SELECT\s+\*', sql_text, re.IGNORECASE):
                            self.log_warning(filename, context, f"SQL {YELLOW}'SELECT *'{RESET} detected. Explicitly listing columns improves 'Query Folding' and memory usage.")

    def _lint_relationships(self):
        rel_path = os.path.join(self.semantic_model_dir, "definition", "relationships.tmdl")
        if not os.path.exists(rel_path): return

        with open(rel_path, "r", encoding="utf-8") as f:
            content = f.read()

        if "crossFilteringBehavior: both" in content:
            self.log_warning("relationships.tmdl", "Relationship", "Bi-directional filter detected. High impact on visual performance.")
        if "fromCardinality: many" in content and "toCardinality: many" in content:
            self.log_warning("relationships.tmdl", "Relationship", "Many-to-Many cardinality detected.")

    def _print_report(self):
        print(f"\n{BOLD}{CYAN}=== POWER BI LINT REPORT ==={RESET}")
        print(f"{BOLD}Project:{RESET} {os.path.basename(self.semantic_model_dir)}\n")
        
        if not self.warnings:
            print(f"{GREEN}[OK]{RESET} No issues found! Your model logic follows best practices.")
        else:
            # Sort warnings by file and context
            self.warnings.sort(key=lambda x: (x['file'], x['context']))
            
            last_file = None
            for w in self.warnings:
                if w['file'] != last_file:
                    print(f"\n{BOLD}{CYAN}# {w['file']}{RESET}")
                    last_file = w['file']
                
                print(f"  {YELLOW}[WRN]{RESET} {BOLD}{w['context']}{RESET}")
                print(f"        {w['message']}")
        
        print(f"\n{BOLD}{CYAN}{'='*36}{RESET}")
        print(f"{BOLD}Total Warnings: {len(self.warnings)}{RESET}\n")

if __name__ == "__main__":
    # Standard check for ANSI support in Windows CMD/PowerShell
    if os.name == 'nt':
        os.system('')
        
    if len(sys.argv) < 2:
        print("Usage: python lint_pbi.py <path_to_pbip_project_root>")
    else:
        PBILinter(sys.argv[1]).lint()
