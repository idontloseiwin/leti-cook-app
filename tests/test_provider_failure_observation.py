"""Safe 5xx diagnosis over the real request path, with no external network."""
import json,unittest
from test_pipeline_provider import ProviderTraceTests
from test_provider_transport_integration import Response

class FailureObservationTests(ProviderTraceTests):
 def test_5xx_reason_is_finite_and_preserves_the_original_response(self):
  cases=[(500,'internalError','provider_internal_error'),(503,'backendError','provider_backend_error'),
         (500,'private-message-token=abc','provider_server_error')]
  for status,reason,expected in cases:
   with self.subTest(status=status,reason=reason):
    emitter,journal=self.observe()
    body=json.dumps({'error':{'message':'private-message-token=abc','errors':[{'reason':reason}]}}).encode()
    self.response=Response(self.gate,chunks=(body,),status=status)
    response=self.request()
    self.assertEqual(response.status_code,status)
    self.assertEqual(response.content,body)
    rows=self.events(emitter,journal)
    finished=[r for r in rows if r['operation']=='http_finished'][-1]
    self.assertEqual(finished['reason'],expected)
    self.assertNotIn('private-message',json.dumps(rows))
    self.assertEqual(emitter.metrics()['invalid'],0)
 def test_non_json_500_stays_a_response_not_a_diagnostic_exception(self):
  emitter,journal=self.observe()
  self.response=Response(self.gate,chunks=(b'<html>private failure</html>',),status=500)
  self.assertEqual(self.request().status_code,500)
  event=next(r for r in self.events(emitter,journal) if r['operation']=='http_finished')
  self.assertEqual(event['reason'],'provider_server_error')
  self.assert_released()

if __name__=='__main__':unittest.main()
