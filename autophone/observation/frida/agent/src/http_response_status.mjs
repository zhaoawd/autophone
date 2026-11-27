// Frida NSInteger may be an Int64 whose JSON representation is a string.
export function httpResponseStatus(response) {
  const status = Number(response.statusCode());
  return Number.isInteger(status) && status >= 100 && status <= 599 ? status : null;
}
