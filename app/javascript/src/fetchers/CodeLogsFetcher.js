import ApiClient from 'src/api_clients/ChemotionApiClient';

export default class CodeLogsFetcher {
  static fetchGenericCodeLogs(data) {
    return ApiClient.getJson(`/api/v1/code_logs/generic?code=${data}`)
      .then((json) => {
        if (json.error) {
          const error = new Error(json.error);
          error.response = json;
          throw error;
        } else {
          return json;
        }
      });
  }

  // Resolves multiple codes (e.g. pasted from a batch of printed labels) in a single request.
  // Returns an array of `{ code, code_log }` or `{ code, error }` entries, one per input code.
  static fetchGenericCodeLogsBatch(codes) {
    return ApiClient.postJson('/api/v1/code_logs/generic_batch', { body: { codes } });
  }
}
