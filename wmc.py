import urllib
import json
import tempfile
import requests
from lxml import etree
import subprocess

import celery
from celery import task

from common import DatabaseTask
import config
import db

from celery.utils.log import get_task_logger
logger = get_task_logger(__name__)


APIBASE = 'http://commons.wikimedia.org/w/api.php?format=xml&action=query&prop=imageinfo&iiprop=sha1|url|thumbmime|extmetadata|archivename&iiurlwidth=640&iilimit=1&maxlag=5'

def login():
    baseurl = "http://commons.wikimedia.org/w/"
    params  = '?action=login&lgname=%s&lgpassword=%s&format=json'% (config.WMC_USER, config.WMC_PASSWORD)

    r = requests.post(baseurl + 'api.php' + params, data={
        'lgname': config.WMC_USER,
        'lgpassword': config.WMC_PASSWORD
    })
    try:
        result = r.json()
        token = result['login']['token']
        login_params = urllib.parse.urlencode({'lgname': config.WMC_USER, 'lgpassword': config.WMC_PASSWORD, 'lgtoken': token})
    except KeyError:
        login_params = ''

    print('setting login params to...', login_params)
    return login_params


def get_metadata(filelist):
    quotedfiles = [urllib.parse.quote(x) for x in filelist]
    apirequest = '%s&titles=%s' % (APIBASE, '|'.join(quotedfiles))
    logger.debug('Requesting %s' % apirequest)

    try:
        r = requests.get(apirequest)
        apidata = etree.fromstring(r.text)
    except (etree.XMLSyntaxError, requests.exceptions.RequestException):
        # TODO: self.retry()
        return

    filedata = {}
    for filename in filelist:
        logger.debug('Traversing API output for %s' % filename)
        filedata[filename] = {}
        try:
            node = apidata.find('.//page[@title="%s"]//ii' % filename)
        except SyntaxError:
            logger.warning('Syntax error on filename %s' % filename)
            continue

        if node is None:
            logger.warning('Returned invalid API data on %s' % filename)
            continue

        filedata[filename]['thumburl'] = apidata.find('.//page[@title="%s"]//ii' % filename).get('thumburl')
        if filedata[filename]['thumburl'] is None:
            logger.warning('Missing thumbnail URL')
            continue

        filedata[filename]['url'] = apidata.find('.//page[@title="%s"]//ii' % filename).get('url')

        filedata[filename]['identifier'] = apidata.find('.//page[@title="%s"]//ii' % filename).get('descriptionurl')
        filedata[filename]['sha1'] = apidata.find('.//page[@title="%s"]//ii' % filename).get('sha1')

        values = {'licenseurl': 'LicenseUrl',
            'licenseshort': 'LicenseShortName',
            'copyrighted': 'Copyrighted',
            'artist': 'Artist',
            'description': 'ImageDescription'}
        for k in values:
            rawnode = apidata.find('.//page[@title="%s"]//ii//%s' % (filename, values[k]))
            if rawnode is not None:
                filedata[filename][k] = rawnode.get('value')

        ''' Check if the file has actually been updated since last time
        we retrieved it or not. We retrieve the file if we don't
        have it in the DB or if the sha1 differ from what we have. '''

    return filedata

@task(bind=True, base=DatabaseTask)
def process(self, args):
    works_apidata, works_hash = args

    work_ids = {work.url: work.id for work in works_apidata}
    filelist = [work.url for work in works_apidata]
    apidata = get_metadata(filelist)

    hash_tasks = []

    for filename in filelist:
        thumburl = apidata[filename].get('thumburl', None)
        work_id = work_ids[filename]

        # set apidata for works here, since we already have the metadata for them
        self.db.query(db.Work).filter_by(id=work_id).\
            update({
                "apidata": json.dumps(apidata[filename]),
                "apidata_status": "done",
            }, synchronize_session=False)

        # and add hashing task to execute them in batch below
        if thumburl is not None:
            hash_tasks.append(wmc_update_hash.s(work_id, thumburl))

    self.db.commit()

    g = celery.group(hash_tasks)

    return g()

@task(bind=True, base=DatabaseTask, rate_limit=config.WMC_RATE_LIMIT)
def wmc_update_hash(self, work_id, image_url):
    tfile = tempfile.NamedTemporaryFile()
    logger.debug('Retrieving %s to %s' % (image_url, tfile.name))

    try:
        r = requests.get(image_url)
        tfile.write(r.content)
    except requests.exceptions.RequestException:
        logger.warning('Unable to retrieve %s' % image_url)
        return None

    try:
        retval = subprocess.check_output([config.BLOCKHASH_COMMAND, tfile.name], universal_newlines=True)
    except (subprocess.CalledProcessError, BlockingIOError):
        logger.debug('%s not supported by blockhash.py' % image_url)
        return None
    else:
        hash = retval.partition(' ')[2]
        hash = hash.strip()
        logger.debug('Blockhash from external cmd: %s' % hash)

    tfile.close()

    if hash is not None:
        self.db.query(db.Work).filter_by(id=work_id).\
            update({
                "hash": hash,
                "hash_status": "done",
            }, synchronize_session=False)
