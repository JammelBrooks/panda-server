import sys

from rucio.client import Client as RucioClient

from pandalogger.PandaLogger import PandaLogger

_logger = PandaLogger().getLogger('configurator_ddm_interface')
_client = RucioClient()
GB = 1024**3

def get_rse_usage(rse, src='srm'):
    """
    Gets disk usage at RSE (Rucio Storage Element)
    """
    method_name = "get_rse_usage <rse={0}>".format(rse)
    _logger.debug('{0} start'.format(method_name))
    
    rse_usage = {}
    try:
        rse_usage_itr = _client.get_rse_usage(rse)
        #Look for the specified information source
        for item in rse_usage_itr:
            if item['source'] == src:
                try:
                    total = item['total']/GB
                except:
                    total = None
                try:
                    used = item['used']/GB
                except:
                    used = None
                try:
                    free = item['free']/GB
                except:
                    free = None
                try:
                    space_timestamp = item['updated_at']
                except:
                    space_timestamp = None
                
                rse_usage = {'total': total, 
                             'used': used, 
                             'free': free, 
                             'space_timestamp': space_timestamp}
                break
    except:
        _logger.error('{0} Excepted with: {1}'.format(method_name, sys.exc_info()))
        return {}
    
    _logger.debug('{0} done {1}'.format(method_name, rse_usage))
    return rse_usage