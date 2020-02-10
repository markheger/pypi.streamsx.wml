# coding=utf-8
# Licensed Materials - Property of IBM
# Copyright IBM Corp. 2020
   
   
from watson_machine_learning_client import WatsonMachineLearningAPIClient
from watson_machine_learning_client.wml_client_error import ApiRequestFailure

from collections import ChainMap   
import logging
   
tracer = logging.getLogger(__name__)   

from streamsx.wml.bundleresthandler import BundleRestHandler   
   
   
_STREAMSX_MAPPING_ERROR_MISSING_MANDATORY = "Missing mandatory input field: "
   
class WmlBundleRestHandler(BundleRestHandler):

    def __init__(self,storage_id, input_queue,wml_client, deployment_guid):

        super().__init__(storage_id, input_queue)
        self._wml_client = wml_client
        self._deployment_guid = deployment_guid
        
    def preprocess(self):
        """WML specific implementation,
        One has to know the fields the model requires as well as the schema of input data.
        Those data is defined through the mapping configuration, set as class variable
        by outer application.
        
        Depending on the framework one need to provide the fields of the names or not.
        
        The payload format for WML scoring is a list of dicts containing "fields" and "values".
        "fields" is a list of fieldnames ordered as the model it requires
        "values" is a 2 dimensional list of multiple scoring data sets, where each set is a list of ordered field values 
        [{"fields": ['field1_name', 'field2_name', 'field3_name', 'field4_name'], 
        "values": [[value1, value2, value3, value4],[value1, value2,  value3, value4]]}]
        
        In case of invalid scoring input WML online scoring will reject the whole bundle with "invalid input" 
        reason without indicating which of the many inputs was wrong!!!
        """
        # this is a sample where all fields are required and are anytime in the input tuple
        # model fields have to be in order/sequence as expected by the model

        assert self.field_mapping is not None
    
        # keep this assert as long as we don't support optional fields
        assert self.allow_optional_fields is False
        
        # clear payload list
        self._payload_list = []
        
        actual_input_combination ={'fields':[],'values':[]}
        for index,_tuple in enumerate(self._data_list):
            tuple_values = []
            tuple_fields = []
            tuple_is_valid = True
            for field in self.field_mapping:
                if field['tuple_field'] in _tuple and _tuple[field['tuple_field']] is not None:
                    tuple_values.append(_tuple[field['tuple_field']])
                    tuple_fields.append(field['model_field'])
                elif self.allow_optional_fields:
                    if field['is_mandatory']:
                        tuple_is_valid = False
                        break
                else:
                    tuple_is_valid = False
                    break

            if tuple_is_valid: 
                self._status_list[index]["mapping_success"] = True
            else:
                self._status_list[index]["mapping_success"] = False
                self._status_list[index]["message"] = _STREAMSX_MAPPING_ERROR_MISSING_MANDATORY + field['tuple_field']
                continue            
                
            if actual_input_combination['fields'] == tuple_fields:
                #same fields as before, just add further values
                actual_input_combination['values'].append(list(tuple_values))
            else:
                #close and store last fields/values combination in final _payload_list
                #except for the first valid tuple being added
                if len(actual_input_combination['values']) > 0 : self._payload_list.append(actual_input_combination) 
                #create new field/value combination
                actual_input_combination['fields']=tuple_fields
                actual_input_combination['values']=[list(tuple_values)]
                        
        #after last tuple store the open field/value combination finally in bundl_list
        self._payload_list.append(actual_input_combination)
    
    
    def synch_rest_call(self):
        rest_success = True
        error_message = None
        
        try:
            if len(self._payload_list) > 0:
                #self._rest_response = self._wml_client.deployments.score(self._deployment_guid,meta_props={'input_data':self._payload_list})
                #testmockup
                self._rest_response = {'predictions': [{'fields': ['prediction$1', 'prediction$2'], 'values': [[0, 1], [1, 2], [2, 3], [3, 4], [4, 5]]}]}
        except wml_client_error.ApiRequestFailure as err:
            """REST request returns 
            400 incase something with the value of 'input_data' is not correct
            404 if the deployment GUID doesn't exists as REST endpoint
                    
            score() function throws in this case an wml_client_error.ApiRequestFailure exception
            with two args: description [0] and the response [1]
            use response.status_code, response.json()["errors"][0]["code"], response.json()["errors"][0]["message"]
                   
            The complete payload is rejected in this case, no single element is referenced to be faulty
            As such we need to write the complete payload to invalid_tuples being submitted to 
            error output port
            """
            tracer.error("WML API error description: %s",str(err.args[0]))
            logger.error("WMLOnlineScoring: WML API error: %s",str(err.args[0]))
            #print("WML REST response headers: ",err.args[1].headers)
            #print("WML REST response statuscode: ",err.args[1].status_code)
            #print("WML REST response code: ",err.args[1].json()["errors"][0]["code"])
            #print("WML REST response message: ",err.args[1].json()["errors"][0]["message"])
            #add the complete local tuple list to invalid list
            #TODO one may think about adding an error indicator if tuple is rejected from mapping function
            #or from scoring as part of a scoring bundle
            #because the predictioon for whole bundle failed, the complete local_list is invalid
            rest_success = False
            error_message = str(err.args[0])
        except:
            tracer.error("Unknown exception: %s", str(sys.exc_info()[0]))
            logger.error("WMLOnlineScoring: Unknown exception: %s", str(sys.exc_info()[0]))
            #because the predictioon for whole bundle failed, the complete local_list is invalid
            rest_success = False
            error_message = str(sys.exc_info()[0])
            
        if rest_success:
            for item in self._status_list:
                if item["mapping_success"]:
                    item["score_success"] = True
        else:
            self._rest_response = None
            for item in self._status_list:
                item["score_success"] = False
                if item["message"] is None:
                    item["message"] = "WML API error: " + error_message

        tracer.debug("WMLOnlineScoring: Worker %d got %d predictions from WML model deployment!", self._storage_id, len(self._rest_response['predictions'][0]['values']))
    
        return self._rest_response['predictions'] # number of prediction response bundles
        

    def postprocess(self):
        if self._rest_response is None:
            #scoring REST call had error, no result to process
            #just the error fields have to be provided
            for index, item in enumerate(self._status_list):
                self._result_list[index] = {"PredictionError": item["message"]}
            return 
            
        for prediction in self._rest_response['predictions']:
            #take the tuples from local list in sequence, sequence is same as the 
            #sequence of prediction 'values' as input was generated in sequence of the _data_list
            #there is no reference from input to prediction except the position in sequence
            #use output mapping function or just add the raw result to tuple
            #for later separation and processing
            #each prediction contains model result 'fields' and one or more 'values' lists
            #one value list for each scoring set

            response_index = 0 # index to result data in the REST response
            
            for data_index,item in enumerate(self._status_list):
                # only data with successful mapping was added in payload and gets response data
                if item["mapping_success"] :
                    self._result_list[data_index] = {"Prediction" : dict(zip(prediction['fields'],prediction['values'][response_index]))}
                    response_index += 1 
                else:
                    self._result_list[data_index] = {"PredictionError": item["message"]}
                    
            '''
            for values in prediction['values']:
                #get a complete dict with field,value for each prediction result
                prediction_dict = dict(zip(prediction['fields'],values))
                # and add it to the stored tuple in local list
                self._result_list[index]['prediction']=prediction_dict
                #submit the default topology tuple for raw Python objects
                self.submit('result_port',{'__spl_po':memoryview(pickle.dumps(local_list[local_list_index]))})
                local_list_index +=1
                send_counter += local_list_index
                tracer.debug("WMLOnlineScoring: Thread %d submitted now % and %d in sum tuples",thread_index, local_list_index, send_counter)
            '''
        
    def get_final_data(self):
        chain = [dict(ChainMap(*[data, result])) for data, result in zip(self._data_list, self._result_list)]
        return chain
