import 'package:flutter/material.dart';

import 'balance_tab.dart';
import 'my_requests_tab.dart';
import 'request_tab.dart';

/// Leave tab: Balance | Request (leave, permission, on duty) | My Requests.
class LeaveScreen extends StatelessWidget {
  const LeaveScreen({super.key, this.initialTab = 0});

  /// 0 Balance, 1 Request, 2 My Requests (e.g. from a decision push).
  final int initialTab;

  @override
  Widget build(BuildContext context) => DefaultTabController(
        length: 3,
        initialIndex: initialTab,
        child: Scaffold(
          appBar: AppBar(
            title: const Text('Leave, Permission & On Duty'),
            bottom: const TabBar(
              tabs: [
                Tab(text: 'Balance'),
                Tab(text: 'Request'),
                Tab(text: 'My Requests'),
              ],
            ),
          ),
          body: const TabBarView(children: [BalanceTab(), RequestTab(), MyRequestsTab()]),
        ),
      );
}
