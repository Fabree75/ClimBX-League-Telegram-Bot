- Implement streak system to encourage participation from students of all levels:
    - Multiplier of points based on streak
    - Show streak in profile
    - Leaderboard of streaks?

- Change registration / profile system:
    - Add level information of each climber
    - Add arkose app profile link
    - Being able to check other people's profiles??
    - If all the above, add menu / options to edit what information is public
    - Add a way for users to visit other climber's profiles (normal students and routesetters only see public info, admins see all info)

- Test route submission with someone else (submission summary appearing in admins' chats automatically, among others)

- Add message "disappearance" flow, so that when in a succession of actions, when clicking open menu buttons, etc, the previous message gets substituted by the new one instead of sending a new message every time. However, slash commands generate a new message (needed since otherwise last message would be the user's). For menus, make it as pages so each page is one of the menus that the user is allowed to see.

- Add route deletion, so anything related to it is reverted (points awarded for ascents, streak multipliers, bonus points, etc.). Make sure making a route inactive mantains the points, streaks, etc.

- For challenges, allow to not submit anything as proof (for example for challenges that require to climb a certain amount of days). Admins still need to accept the submission even if no visual proof is required

- After creating a route or a challenge, add a button to the message, along with the "open menu" one already there, that goes to the list of all routes / challenges.

- For certain moments where the user can cancel an action, add the option to go back to the previous message state instead of cancelling (which resets all the progress within that action). In any case, whenever a user can type /cancel to cancel an action, add the corresponding gui button to the message (in some of them the button is still not there)

- Check that if a routesetter is present in the .env but not added with the bot, then it still has routesetter access or it is just a "fallback". 