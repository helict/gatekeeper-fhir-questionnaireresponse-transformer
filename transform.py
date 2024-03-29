#!/usr/bin/env python3
import argparse
import csv
import json
import logging
import re
import unicodedata
from datetime import datetime
from pathlib import Path

def fhir_coding_type(arg_value, pattern = re.compile(r'^.+\|.+$')):
  if pattern.match(arg_value):
    return arg_value

  raise argparse.ArgumentTypeError

arg_parser = argparse.ArgumentParser(description = 'Transform FHIR Bundle with QuestionnaireResponse entries to CSV')
arg_parser.add_argument('-c', '--codes', help='Path to FHIR ConceptMap file with answer codes', type=Path, default=None)
arg_parser.add_argument('-d', '--dialect', help='Dialect used to format CSV', type=str, default='excel', choices=['excel', 'excel-tab', 'unix'])
arg_parser.add_argument('-l', '--logfile', help='Path to log file', type=Path, default=Path('./output.log'))
arg_parser.add_argument('-o', '--output', help='Path to CSV output file', type=Path, default=Path('./output.csv'))
arg_parser.add_argument('-t', '--tag', help='Resource tag for survey time formatted as \"system|code\"', type=fhir_coding_type, default=None)
arg_parser.add_argument('-n', '--names', help='Variable names (linkIds) mappings', type=Path, default=None)
arg_parser.add_argument('-v', '--verbosity', help='Verbosity of output', type=str, default='INFO', choices=['INFO', 'WARNING', 'DEBUG'])
arg_parser.add_argument('bundle', help='Path to FHIR JSON Bundle input file', type=Path)
args = arg_parser.parse_args()

logging.basicConfig(
  filename=args.logfile, 
  filemode='w',
  encoding='utf-8', 
  level=logging.getLevelName(args.verbosity),
  format='%(asctime)s %(levelname)-8s %(message)s',
  datefmt='%Y-%m-%d %H:%M:%S'
)

def load_variable_names(mapping_file_path):
  if not mapping_file_path:
    return None

  variable_names = {}

  logging.info('Load variable name mappings from CSV file')
  with open(mapping_file_path, 'r') as csv_file:
    reader = csv.DictReader(
      csv_file, 
      dialect = args.dialect
    )

    for row in reader:
      qid = row['questionnaire']
      src = row['source']
      tgt = row['target']

      if qid not in variable_names:
        variable_names[qid] = {}

      if src not in variable_names[qid]:
        variable_names[qid][src] = tgt
      
  return variable_names

def load_answer_codes(answer_codes):
  if not answer_codes:
    return None

  code_map = {}

  logging.info('Load FHIR ConceptMap answer codes from file')
  with open(answer_codes) as json_file:
    concept_map = json.load(json_file)
  
  for group in concept_map['group']:
    source = group['source']
    logging.debug('Create answer code map for {}'.format(source))

    for element in group['element']:
      code_from = element['code']
      code_to = element['target'][0]['code']
      logging.debug('Map answer code from \'{}\' to \'{}\''.format(code_from, code_to))

      if not (source in code_map):
        code_map[source] = {}

      code_map[source][code_from] = code_to

  return code_map

# Removes non printable ASCII characters from string
def sanitize_str(s):
  return ''.join(ch for ch in s if not unicodedata.category(ch).startswith('C'))

def to_str(answer):
  res = ''

  if type(answer) == dict:
    if ('valueBoolean' in answer):
      res = str(answer['valueBoolean']).lower()
    elif ('valueCoding' in answer):
      if ('code' in answer['valueCoding']):
        res = answer['valueCoding']['code']
      else:
        logging.warning('Missing \'code\' for valueCoding in answer')
    elif ('valueQuantity' in answer):
      res = answer['valueQuantity']['value']
      if ('comparator' in answer['valueQuantity']):
        comp = answer['valueQuantity']['comparator']
        res = '{}{}'.format(comp, res)
    else:
      res = to_str(answer[next(iter(answer))])
  else:
    res = str(answer)

  return sanitize_str(res) if type(res) == str else res

def extract_answers(questionnaire, items, answer_codes):
  extracted_answers = {}

  for item in items:
    link_id = item['linkId']
    answer_code_source = '{}|{}'.format(questionnaire, link_id)
    answer_code_source_shared = '{}|*'.format(questionnaire)

    if 'answer' in item:
      answers = item['answer']
      has_multiple_answers = (len(answers) > 1)

      if has_multiple_answers:
        logging.info('Found multiple answers: {}|{}'.format(questionnaire, link_id))

        for answer in answers:
          answer = to_str(answer)
          answer_code_target = None
          
          if answer_code_source in answer_codes:
            if answer in answer_codes[answer_code_source]:
              # Found coding for answer in dict
              answer_code_target = answer_codes[answer_code_source][answer]
            elif '*' in answer_codes[answer_code_source]:
              # Found * as coding for answer in dict
              answer_code_target = answer_codes[answer_code_source]['*']
          elif (answer_code_source_shared in answer_codes) and (answer in answer_codes[answer_code_source_shared]):
            # Found coding for answer in shared coding dict
            answer_code_target = answer_codes[answer_code_source_shared][answer]

          if answer_code_target:
            variable = '{}.{}'.format(link_id, answer_code_target)
            logging.debug('Perform answer coding for answer: {}|{}'.format(variable, answer))
            extracted_answers.update({variable: 1})
          else:
            logging.warning('Skip answer coding for multiple answer: {}|{}'.format(link_id, answer))

      else:
        for answer in answers:
          answer = to_str(answer)

          if answer_codes and (answer_code_source in answer_codes) and (answer in answer_codes[answer_code_source]):
            logging.debug('Perform answer coding for answer: {}|{}'.format(link_id, answer))
            extracted_answers.update({link_id: answer_codes[answer_code_source][answer]})
          elif answer_codes and (answer_code_source in answer_codes) and ('*' in answer_codes[answer_code_source]):
            logging.debug('Perform answer coding for answer: {}|{}'.format(link_id, answer))
            extracted_answers.update({link_id: answer_codes[answer_code_source]['*']})
          elif answer_codes and (answer_code_source_shared in answer_codes) and (answer in answer_codes[answer_code_source_shared]):
            logging.debug('Perform answer coding for answer: {}|{}'.format(link_id, answer))
            extracted_answers.update({link_id: answer_codes[answer_code_source_shared][answer]})
          else:
            logging.warning('Skip answer coding for answer: {}|{}'.format(link_id, answer))
            extracted_answers.update({link_id: answer})
    
    if 'item' in item:
        logging.info('Process nested items for item {}'.format(item['linkId']))
        extracted_answers.update(extract_answers(questionnaire, item['item'], answer_codes))

  return extracted_answers

def has_tag(entry, tag_arg):
  resource = entry['resource']
  logging.debug('Check {} for tag \"{}\"'.format(entry['fullUrl'], tag_arg))

  if ('meta' in resource) and ('tag' in resource['meta']):
    system, code = tag_arg.split('|')

    for tag in resource['meta']['tag']:
      if (tag['system'] == system) and (tag['code'] == code):
        logging.debug('Found tag \"{}\" in {} '.format(tag_arg, entry['fullUrl']))
        return True

  logging.debug('Cannot find tag \"{}\" in {} '.format(tag_arg, entry['fullUrl']))
  return False

def get_tags(resource):
  result = {}

  if ('meta' in resource) and ('tag' in resource['meta']):
    for tag in resource['meta']['tag']:
      result.update({ tag['system']: tag['code']})

  return result

def get_tag_prefix(resource):
  tags = get_tags(resource)
  return ''.join(['{}_'.format(tag.upper()) for tag in tags.values()]) if tags else ''

def main():
  logging.info('Load FHIR Bundle from file')
  with open(args.bundle) as json_file:
    bundle = json.load(json_file)
  
  logging.info('Read FHIR Bundle containing {} of {} entries'.format(len(bundle['entry']), bundle['total']))
  logging.info('Extract FHIR QuestionnaireResponse entries from FHIR Bundle')
  
  entries = []
  answers = []
  answer_codes = load_answer_codes(args.codes)
  variable_names = load_variable_names(args.names)
  subjects = set()

  # Filter tagged bundle entries for further processing
  if args.tag:
    entries = list(filter(lambda entry: has_tag(entry, args.tag), bundle['entry']))
    logging.debug('Filtered {} of {} bundle entries with tag {}'.format(len(entries), len(bundle['entry']), args.tag))
  else:
    for entry in bundle['entry']:
      # Check resource type and existence of subject
      if entry['resource']['resourceType'] == 'QuestionnaireResponse':
        if not 'subject' in entry['resource']:
          logging.warning('Skip processing of resource {} because of missing subject'.format(entry['fullUrl']))
        elif not 'reference' in entry['resource']['subject']:
          logging.warning('Skip processing of resource {} because only subject references can be handled'.format(entry['fullUrl']))
        else:
          entries.append(entry)
      else:
        logging.debug('Skip processing of resource {} because of resource type mismatch'.format(entry['fullUrl']))

  # Transform each bundle entry resource (QuesionnaireResponse)
  for entry in entries:
    resource = entry['resource']
    subject = resource['subject']['reference']
    questionnaire = resource['questionnaire']
    date = resource['authored']
    items = resource['item']
    subjects.add(subject)
    answer = {
      'id': subject,
      'questionnaire': questionnaire,
      'name': re.sub(r'^.*/(.*)$', r'\1', questionnaire),
      'date': date,
      'tag': get_tag_prefix(resource),
      'items': {}
    }
  
    logging.debug('Process FHIR QuestionnaireResponse resource {} for questionnaire {}'.format(entry['fullUrl'], resource['questionnaire']))
    if len(items) == 0:
      logging.warning('Skip processing of resource {} because no item available'.format(entry['fullUrl']))
    else:
      answer['items'] = extract_answers(questionnaire, items, answer_codes)
      answers.append(answer)

  # Apply tag (or empty tag) to linkIds and date
  for answer in answers:
    tag = answer['tag']
    name = answer['name']
    questionnaire = answer['questionnaire']
    tagged_items = {}
    
    # Format authoring date and prepend as item
    date = answer['date']
    date_variable = '{}{}_Date'.format(tag, name)
    tagged_items[date_variable] = datetime.strptime(date, '%Y-%m-%dT%H:%M:%S.%fZ').strftime('%d.%m.%Y')

    # Apply tag as prefix to linkIds and variable name mappings
    for link_id in answer['items'].keys():
      variable_name = None

      if questionnaire in variable_names and link_id in variable_names[questionnaire]:
          variable_name = variable_names[questionnaire][link_id]
          logging.debug('Perform variable name mapping for questionnaire {} from {} to {}'.format(questionnaire, link_id, variable_name))
      else:
        logging.warning('Skip variable name mapping for questionnaire {}, item {}'.format(questionnaire, link_id))

      new_link_id = '{}{}'.format(tag, variable_name) if variable_name else '{}{}'.format(tag, link_id)
      tagged_items[new_link_id] = answer['items'][link_id]

    del answer['items']
    answer['items'] = tagged_items

  # Flatten the answers to row-column format
  logging.info('Flatten the structure of FHIR QuestionnaireResponse entries')
  rows = []
  
  for subject in subjects:
    row = {'id': subject}

    for answer in answers:
      if answer['id'] == subject:
        for key in answer['items'].keys():
          row[key] = answer['items'][key]
      
    rows.append(row)
  
  logging.info('Extract unique column headers')
  headers = {}
  
  for row in rows:
    for key in row.keys():
      headers[key] = None
  
  logging.debug(headers)
  logging.info('Write CSV data to file')
  with open(args.output, 'w') as csv_file:
    writer = csv.DictWriter(
      csv_file, 
      fieldnames = headers.keys(), 
      dialect = args.dialect
    )
    writer.writeheader()
    writer.writerows(rows)

if __name__ == '__main__':
  main()