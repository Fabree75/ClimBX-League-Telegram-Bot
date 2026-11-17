- Implement streak system to encourage participation from students of all levels:
    - Multiplier of points based on streak
    - Show streak in profile
    - Leaderboard of streaks?

- Remove toggle mode, just keep the button menu and raw text commands to coexist

- Change registration / profile system:
    - Add confirmation message in registration with all info before sending request, warning that full name and email won't be able to be modified
    - Remove re-registration system
    - Change username to be optional (if not indicated, take the full name)
    - Add level information of each climber
    - Add arkose app profile link
    - Being able to check other people's profiles??
    - If all the above, add menu / options to edit what information is public
    - In any case, allow the user to modify info in his profile (for important info such as full name and email, the user sends a request to admins with the changes)
    - Add a way for users to visit other climber's profiles (normal students and routesetters only see public info, admins see all info)

- Test route submission with someone else (submission summary appearing in admins' chats automatically, among others)

- Change email regex in registration to check that the mail is a polytechnique one (@polytechnique.edu)

- Allow admins to re-register without sending a request

- Separate /help command in /rules and /commands, then set /help as a general guide that indicates what to do (refers to /rules, /commands, etc.)

- Add routesetter role, that does not have admin functionalities but can add new routes with the points to the contest (change /admin command with /role or similar)

- Add route info modification (name, points, zones, pictures, etc) after it has already been created. Include add pictures within this option.

- When executing commands that do not require further action or at the end of a chain of steps for a command, add a button in the last message that shows the gui menu when clicked (right now we need to write /menu every time)

- Solve bug when indicating partial points for a route if full points is 1, it says "enter a whole number between 1 and 0"

- Remove "Custom Partial Points" option to give custom points for a submission, and only allow the full points or the points given by an already established zone. If the route has no zone, then only allow to approve with full points or reject.