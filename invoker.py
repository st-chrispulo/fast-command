from commands import SayHelloCommand, LoginCommand, CreateUserCommand, LogoutCommand, \
    RefreshTokenCommand, MeCommand, UploadUserAvatarCommand, GenerateSecureDownloadLinkCommand, AddRoleCommand, \
    AssignRoleToUserCommand, GetCommandNamesCommand, SyncPermissionCommand, TransformTemplateRenderCommand, \
    RegisterGithubUserCommand, GithubSignInCommand, GithubStartCommand, CreateCompContentsWithUploadsCommand, \
    UpdateComponentWithUploadsCommand, DeleteCompContentCommand, ContentGetCommand

command_registry = [
    CreateUserCommand(),
    LoginCommand(),
    SayHelloCommand(),
    RefreshTokenCommand(),
    LogoutCommand(),
    MeCommand(),
    UploadUserAvatarCommand(),
    GenerateSecureDownloadLinkCommand(),
    AddRoleCommand(),
    AssignRoleToUserCommand(),
    GetCommandNamesCommand(),
    SyncPermissionCommand(),
    TransformTemplateRenderCommand(),
    RegisterGithubUserCommand(),
    GithubSignInCommand(),
    GithubStartCommand(),
    CreateCompContentsWithUploadsCommand(),
    UpdateComponentWithUploadsCommand(),
    DeleteCompContentCommand(),
    ContentGetCommand()
]


