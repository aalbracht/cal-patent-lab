#!/usr/bin/env python
import HTMLParser  # To unescape HTML escape sequences in patent claims
import json
import MySQLdb
from os import listdir
from os.path import isfile, join
import notebooks.parser_new_data as parser
import re
import signal
import sys

DB_HOST = "cal-patent-lab.chhaitskv8dz.us-west-2.rds.amazonaws.com"
DB_USER = "***REMOVED***"
DB_PASSWORD = None  # Get this field from the user
DB_NAME = "***REMOVED***"
DB_TABLE = "patents_decision"
db_conn = None
html_parser = None

def db(host, username, psswd, database_name):
    return MySQLdb.connect(host, username, psswd, database_name)


def write_data_to_db(cursor, table_name, insert_queue):
    # SQL query to add a new row containing patent id and deciscion
    # the invalidated field of the table takes an int as an input
    query_placeholder = ",".join(["(%s, %s, %s)"] * len(insert_queue))
    sql_query = ('INSERT INTO {} (patent_id, invalidated, claims_text) VALUES '
       '{}'.format(table_name, query_placeholder))
    data_values = []
    for line in insert_queue:
        for item in line:
            data_values.append(item)
    cursor.execute(sql_query, data_values)


# Return a list of fields corresponding to those in the destination DB table
def build_line(patent_id, claims_text, decision_table):
    line = [patent_id]
    for field in ["invalidated"]:
        line.append(decision_table.get(patent_id, {}).get(field))
    line.append(claims_text)
    return line


# Try to commit to DB, and return whether this operation succeeded
def commit_pending_db_data(db_conn):
    try:
        print("Committing to DB")
        db_conn.commit()
        return True
    except:
        print("WARNING: error committing to DB")
        db_conn.rollback()
        return False


def handle_ctrl_c(signal, frame):
    print("SIGINT caught, committing to DB and shutting down")
    status = commit_pending_db_data(db_conn)
    if not status:
        print("WARNING: error committing to DB")
    sys.exit(1)


# Load file containing merged PTAB API data and decisions
def load_merged_file(filename):
    with open(filename) as fd:
        return json.load(fd)


# Splits the given TSV line into patent ID and claims
def split_patent_line(line):
    # isolate patent #, claims text
    patent_id, patent_body = line.split('\t')
    patent_id = patent_id.strip()
    _, claims_text = patent_body.split('CLAIMS. ', 1)
    # Strip out any invalid ASCII to avoid string decode issues
    claims_text = claims_text.encode("ascii", errors="ignore")
    
    global html_parser
    if html_parser is None:
        html_parser = HTMLParser.HTMLParser()
    claims_text = html_parser.unescape(claims_text).encode("utf-8")
    return patent_id, claims_text


def flush_insert_queue(cursor, queue):
    if len(queue) > 0:
        print("Flushing batch")
        write_data_to_db(cursor, DB_TABLE, queue)


def record_file_as_done(save_filename, filename_to_record):
    save_fd = open(save_filename, "a")
    print("Writing {} to save file".format(filename_to_record))
    save_fd.write("{}\n".format(filename_to_record))
    save_fd.flush()
    save_fd.close()


def main():
    argv = sys.argv[1:]
    if len(argv) not in (2, 3):
        print("Usage: populate_db.py path/to/merged_file "
              "path/to/patent_tsv_files "
              "[path/to/save_file]")
        sys.exit(1)
    
    # Catch SIGINT (e.g. generated by Ctrl-C on the keyboard)
    signal.signal(signal.SIGINT, handle_ctrl_c)
    
    src_filename = argv[0]
    tsvdir = argv[1]
    save_filename = None
    done = []
    # Get save file and read its contents, if one was provided
    if len(argv) == 3:
        save_filename = argv[2]
        if isfile(save_filename):
            with open(save_filename) as fd:
                done = [line.strip() for line in fd.readlines() if len(line.strip()) > 0]
    tsvfiles = [join(tsvdir, f) for f in listdir(tsvdir) if isfile(join(tsvdir, f))]
    tsvfiles = list(set(tsvfiles) - set(done))
    
    # Prompt for database password
    global DB_PASSWORD
    print("Please enter the database password: ")
    DB_PASSWORD = sys.stdin.readline().strip()
    
    global db_conn
    db_conn = db(DB_HOST, DB_USER, DB_PASSWORD, DB_NAME)
    cursor = db_conn.cursor()
    
    # Get list of all patent IDs in DB
    print("Getting all existing patent IDs in DB")
    cursor.execute("select patent_id from patents_decision")
    existing_IDs = set([elem[0] for elem in cursor.fetchall()])
    
    # Load merged file with decisions and other attributes
    decision_table = load_merged_file(src_filename)
    
    for tsv_filename in tsvfiles:
        print("Reading patents from {}".format(tsv_filename))
        claims_file = open(tsv_filename, 'r')
        insert_queue = []
        counter = 0
        for line in claims_file:
            counter += 1
            if counter & 0x3ff == 0:
                print("Reading line {}".format(counter))
            
            patent_id, claims_text = split_patent_line(line)
            # Some entries in the TSV files are published applications, e.g. 2004/0204653
            # Skip these, since we only want granted patents
            if patent_id in existing_IDs or "/" in patent_id:
                continue
            
            line = build_line(patent_id, claims_text, decision_table)
            insert_queue.append(line)
            existing_IDs.add(patent_id)
            if len(insert_queue) == 80:
                flush_insert_queue(cursor, insert_queue)
                insert_queue = []
        flush_insert_queue(cursor, insert_queue)
        insert_queue = []
        # Commit transaction after finishing every TSV file
        commit_pending_db_data(db_conn)
        # Add TSV file to list of completed files
        record_file_as_done(save_filename, tsv_filename)
    
    # Write remaining patents that did not appear in any TSV file
    insert_queue = []
    for patent_id in decision_table:
        if patent_id in existing_IDs or "/" in patent_id:
            continue
        line = build_line(patent_id, None, decision_table)  # No claims text here
        insert_queue.append(line)
        existing_IDs.add(patent_id)
        if len(insert_queue) == 80:
            flush_insert_queue(cursor, insert_queue)
            insert_queue = []
    flush_insert_queue(cursor, insert_queue)
    commit_pending_db_data(db_conn)
    db_conn.close()


if __name__ == "__main__":
    main()
