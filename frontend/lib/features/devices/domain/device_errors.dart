/// Plain-language texts for the device error codes (api-contract.md 21.2, 21.7).
/// Job-level codes come in `DeviceSync.error_code` / `Device.last_error_code`;
/// the HTTP ones in the error envelope. No technical wording reaches the user.
library;

/// Message for a device-related [code], or null when the code is not one of them.
String? deviceErrorText(String? code) => switch (code) {
      'DEVICE_UNREACHABLE' =>
        'The machine could not be reached. Check that it is switched on and on the network, '
            'and that the IP address and port are right.',
      'DEVICE_TIMEOUT' => 'The machine did not answer in time. Check the network and try again.',
      'DEVICE_AUTH_FAILED' =>
        'The machine did not accept the communication key. Check the key set on the machine.',
      'DEVICE_BUSY' =>
        'Another program is connected to the machine right now. Close it or wait a few minutes, then try again.',
      'DEVICE_PROTOCOL_ERROR' =>
        'The machine sent a reply that was not understood. Check the TCP / UDP setting and the port, then try again.',
      'DEVICE_IDENTITY_CHANGED' =>
        'This looks like a different machine than before. If it was replaced on purpose, '
            'open the device and accept the new identity.',
      'DEVICE_CLOCK_INVALID' =>
        "The machine's clock is far off, so its punches are not used. Correct the time on the machine.",
      'CIRCUIT_OPEN' =>
        'Syncing is paused for a while after repeated failures. It will try again by itself.',
      'DEVICE_ADDRESS_NOT_ALLOWED' =>
        "This address is not allowed. Use the machine's address on the office network.",
      'DEVICE_DISABLED' => 'This device is switched off. Enable it first.',
      'TEMPORARILY_UNAVAILABLE' =>
        'The sync service is not running right now. Please try again in a few minutes.',
      'INVALID_DATE_RANGE' => 'Choose a date range of at most 92 days, with the start before the end.',
      _ => null,
    };

/// Text for a failed job: the known code, else the server's own wording, else a generic line.
String deviceJobErrorText(String? code, [String? serverMessage]) =>
    deviceErrorText(code) ??
    ((serverMessage ?? '').trim().isNotEmpty ? serverMessage!.trim() : 'The sync did not finish. Please try again.');
