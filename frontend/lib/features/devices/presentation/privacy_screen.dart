import 'package:flutter/material.dart';

import '../domain/device_models.dart';

/// More -> Privacy: what the office attendance machines share with the app.
class PrivacyScreen extends StatelessWidget {
  const PrivacyScreen({super.key});

  @override
  Widget build(BuildContext context) => Scaffold(
        appBar: AppBar(title: const Text('Privacy')),
        body: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Icon(Icons.privacy_tip_outlined, color: Theme.of(context).colorScheme.primary),
                const SizedBox(width: 12),
                const Expanded(child: Text(devicePrivacyNotice, key: ValueKey('privacy-notice'))),
              ],
            ),
          ],
        ),
      );
}
